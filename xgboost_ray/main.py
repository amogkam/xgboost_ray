import os
import time
from threading import Thread
from typing import Tuple, Dict, Any, List, Optional

from ray.exceptions import RayActorError

try:
    import ray
    from ray import logger
    from ray.services import get_node_ip_address
    RAY_INSTALLED = True
except ImportError:
    ray = None
    logger = None
    get_node_ip_address = None
    RAY_INSTALLED = False

import xgboost as xgb


from xgboost_ray.matrix import RayDMatrix


def _assert_ray_support():
    if not RAY_INSTALLED:
        raise ImportError(
            'Ray needs to be installed in order to use this module. '
            'Try: `pip install ray`')


def _start_rabit_tracker(num_workers: int):
    """Start Rabit tracker. The workers connect to this tracker to share
    their results."""
    host = get_node_ip_address()

    env = {'DMLC_NUM_WORKER': num_workers}
    rabit_tracker = xgb.RabitTracker(hostIP=host, nslave=num_workers)

    # Get tracker Host + IP
    env.update(rabit_tracker.slave_envs())
    rabit_tracker.start(num_workers)

    # Wait until context completion
    thread = Thread(target=rabit_tracker.join)
    thread.daemon = True
    thread.start()

    return env


class RabitContext:
    """Context to connect a worker to a rabit tracker"""
    def __init__(self, actor_id, args):
        self.args = args
        self.args.append(
            ('DMLC_TASK_ID=[xgboost.ray]:' + actor_id).encode())

    def __enter__(self):
        xgb.rabit.init(self.args)

    def __exit__(self, *args):
        xgb.rabit.finalize()


def _checkpoint_file(path: str, prefix: str, rank: int):
    if not prefix:
        return None
    return os.path.join(path, f"{prefix}_{rank:05d}.xgb")


@ray.remote
class RayXGBoostActor:
    def __init__(self,
                 rank: int,
                 num_actors: int,
                 checkpoint_prefix: Optional[str] = None,
                 checkpoint_path: str = "/tmp",
                 checkpoint_frequency: int = 5):
        self.rank = rank
        self.num_actors = num_actors

        self.checkpoint_prefix = checkpoint_prefix
        self.checkpoint_path = checkpoint_path
        self.checkpoint_frequency = checkpoint_frequency

        self._data: Dict[RayDMatrix, xgb.DMatrix] = {}
        self._evals = []

    @property
    def checkpoint_file(self) -> Optional[str]:
        return _checkpoint_file(
            self.checkpoint_path, self.checkpoint_prefix, self.rank)

    @property
    def _save_checkpoint_callback(self):
        def callback(env):
            if env.iteration % self.checkpoint_frequency == 0:
                env.model.save_model(self.checkpoint_file)
        return callback

    def load_data(self, data: RayDMatrix):
        x, y = ray.get(data.load_data(self.rank, self.num_actors))
        matrix = xgb.DMatrix(x, label=y)
        self._data[data] = matrix

    def train(self,
              rabit_args: List[str],
              params: Dict[str, Any],
              dtrain: RayDMatrix,
              evals: Tuple[RayDMatrix, str],
              *args,
              **kwargs) -> Dict[str, Any]:
        local_params = params.copy()

        if dtrain not in self._data:
            self.load_data(dtrain)
        local_dtrain = self._data[dtrain]

        local_evals = []
        for deval, name in evals:
            if deval not in self._data:
                self.load_data(deval)
            local_evals.append((self._data[deval], name))

        evals_result = dict()

        # Load model
        if os.path.exists(self.checkpoint_file):
            kwargs.update({"xgb_model": self.checkpoint_file})

        if "callbacks" in kwargs:
            callbacks = kwargs["callbacks"]
        else:
            callbacks = []
        callbacks.append(self._save_checkpoint_callback)
        kwargs["callbacks"] = callbacks

        with RabitContext(str(id(self)), rabit_args):
            bst = xgb.train(
                local_params,
                local_dtrain,
                *args,
                evals=local_evals,
                evals_result=evals_result,
                **kwargs
            )
            return {"bst": bst, "evals_result": evals_result}


def _create_actor(
        rank: int,
        num_actors: int,
        num_gpus_per_worker: int,
        checkpoint_prefix: Optional[str] = None,
        checkpoint_path: str = "/tmp",
        checkpoint_frequency: int = 5):
    return RayXGBoostActor.options(num_gpus=num_gpus_per_worker).remote(
        rank=rank,
        num_actors=num_actors,
        checkpoint_prefix=checkpoint_prefix,
        checkpoint_path=checkpoint_path,
        checkpoint_frequency=checkpoint_frequency)


def _trigger_data_load(actor, dtrain, evals):
    wait_load = [actor.load_data.remote(dtrain)]
    for deval, name in evals:
        wait_load.append(actor.load_data.remote(deval))
    return wait_load


def _cleanup(
    checkpoint_prefix: str,
    checkpoint_path: str,
    num_actors: int):
    for i in range(num_actors):
        checkpoint_file = _checkpoint_file(
            checkpoint_path, checkpoint_prefix, i)
        if os.path.exists(checkpoint_file):
            os.remove(checkpoint_file)


def _train(
        params: Dict,
        dtrain: RayDMatrix,
        *args,
        evals=(),
        num_actors: int = 4,
        gpus_per_worker: int = -1,
        checkpoint_prefix: Optional[str] = None,
        checkpoint_path: str = "/tmp",
        checkpoint_frequency: int = 5,
        **kwargs):
    _assert_ray_support()

    if not ray.is_initialized():
        ray.init()

    if gpus_per_worker == -1:
        gpus_per_worker = 0
        if "tree_method" in params and params["tree_method"].startswith("gpu"):
            gpus_per_worker = 1

    # Create remote actors
    actors = [
        _create_actor(
            i, num_actors, gpus_per_worker,
            checkpoint_prefix, checkpoint_path, checkpoint_frequency)
        for i in range(num_actors)
    ]
    logger.info(f"[RayXGBoost] Created {len(actors)} remote actors.")

    # Split data across workers
    wait_load = []
    for _, actor in enumerate(actors):
        wait_load.extend(_trigger_data_load(actor, dtrain, evals))

    ray.get(wait_load)

    logger.info("[RayXGBoost] Starting XGBoost training.")

    # Start tracker
    env = _start_rabit_tracker(num_actors)
    rabit_args = [('%s=%s' % item).encode() for item in env.items()]

    # Train
    fut = [
        actor.train.remote(rabit_args, params, dtrain, evals, *args, **kwargs)
        for actor in actors
    ]

    ray.get(fut)

    # All results should be the same because of Rabit tracking. So we just
    # return the first one.
    res: Dict[str, Any] = ray.get(fut[0])
    bst = res["bst"]
    evals_result = res["evals_result"]

    if checkpoint_prefix:
        _cleanup(checkpoint_prefix, checkpoint_path, num_actors)

    return bst, evals_result


def train(
        params: Dict,
        dtrain: RayDMatrix,
        *args,
        evals=(),
        num_actors: int = 4,
        gpus_per_worker: int = -1,
        max_actor_restarts: int = 0,
        **kwargs):
    """Test

    Args:
        params (Dict): parameter dict passed to `xgboost.train()`
        dtrain (RayDMatrix): Data object containing the training data.
        evals (Union[List[Tuple], Tuple]): `evals` tuple passed to
            `xgboost.train()`.
        num_actors (int): Number of parallel Ray actors.
        gpus_per_worker (int): Number of GPUs to be used per Ray actor.
        max_actor_restarts (int): Number of retries when Ray actors fail.
            Defaults to 0 (no retries). Set to -1 for unlimited retries.

    Keyword Args:
        checkpoint_prefix (str): Prefix for the checkpoint filenames.
            Defaults to `.xgb_ray_{time.time()}`.
        checkpoint_path (str): Path to store checkpoints at. Defaults to
            `/tmp`
        checkpoint_frequency (int): How often to save checkpoints. Defaults
            to 5.
    """
    max_actor_restarts = max_actor_restarts \
        if max_actor_restarts >= 0 else float("inf")
    _assert_ray_support()

    checkpoint_prefix = kwargs.get(
        "checkpoint_prefix", f".xgb_ray_{time.time()}")
    checkpoint_path = kwargs.get("checkpoint_path", "/tmp")
    checkpoint_frequency = kwargs.get("checkpoint_frequency", 5)

    tries = 0
    while tries <= max_actor_restarts:
        try:
            return _train(
                params,
                dtrain,
                *args,
                evals=evals,
                num_actors=num_actors,
                gpus_per_worker=gpus_per_worker,
                checkpoint_prefix=checkpoint_prefix,
                checkpoint_path=checkpoint_path,
                checkpoint_frequency=checkpoint_frequency,
                **kwargs
            )
        except RayActorError:
            if tries+1 <= max_actor_restarts:
                logger.warning(
                    "A Ray actor died during training. Trying to restart "
                    "and continue training from last checkpoint.")
            else:
                raise RuntimeError(
                    "A Ray actor died during training and the maximum number "
                    "of retries is exhausted. Checkpoints have been stored "
                    "at `{}` with prefix `{}` - you can pass these parameters "
                    "as `checkpoint_path` and `checkpoint_prefix` to the "
                    "`train()` function to try to continue "
                    "the training.".format(
                        checkpoint_path, checkpoint_frequency))
            tries += 1
    return None, {}
