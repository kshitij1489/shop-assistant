"""One lazily loaded, pinned encoder per process."""
import os
import threading

from .config import policy

_MODEL = None
_SPEC = None
_LOCK = threading.Lock()


def get_sbert_model():
    global _MODEL, _SPEC
    config = policy()
    spec = (config.model, config.revision, config.dimension)
    with _LOCK:
        if _MODEL is not None and _SPEC != spec:
            raise RuntimeError("Embedding configuration changed; restart the worker")
        if _MODEL is None:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(
                config.model, revision=config.revision, device="cpu", trust_remote_code=False,
                cache_folder=os.getenv("HF_HOME", os.getenv("TRANSFORMERS_CACHE", "/model_cache")),
            )
            if model.get_sentence_embedding_dimension() != config.dimension:
                raise ValueError("Configured embedding dimension does not match encoder")
            _MODEL, _SPEC = model, spec
        return _MODEL
