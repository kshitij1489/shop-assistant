import os, logging

log = logging.getLogger(__name__)
_MODEL = None

def get_sbert_model():
    """Load once on first use, outside Celery's child-startup timeout."""
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        name = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        cache_dir = os.getenv("HF_HOME", os.getenv("TRANSFORMERS_CACHE", "/model_cache"))
        _MODEL = SentenceTransformer(name, cache_folder=cache_dir, device="cpu")
        log.info("SBERT model created in PID=%s, cache=%s", os.getpid(), cache_dir)
    return _MODEL
