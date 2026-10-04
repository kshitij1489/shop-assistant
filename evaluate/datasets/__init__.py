"""Dataset loading and strict, offline validation."""
from .loader import DatasetBundle, DatasetError, load_dataset

__all__ = ["DatasetBundle", "DatasetError", "load_dataset"]
