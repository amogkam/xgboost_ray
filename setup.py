from setuptools import find_packages, setup

setup(
    name="xgboost_ray",
    packages=["xgboost_ray"],
    version="0.0.1",
    author="Ray Team",
    description="A Ray backend for distributed XGBoost",
    long_description="A distributed backend for XGBoost built on top of "
                     "distributed computing framework Ray.",
    url="https://github.com/ray-project/xgboost_ray",
    install_requires=[
        "xgboost", "ray", "numpy>=1.16", "pandas", "pytest"
    ])
