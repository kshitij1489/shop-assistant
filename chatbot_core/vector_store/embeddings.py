import os, logging
from celery import signals

log = logging.getLogger(__name__)
_MODEL = None

def get_sbert_model():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        name = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        cache_dir = os.getenv("HF_HOME", os.getenv("TRANSFORMERS_CACHE", "/model_cache"))
        _MODEL = SentenceTransformer(name, cache_folder=cache_dir, device="cpu")
        log.info("SBERT model created in PID=%s, cache=%s", os.getpid(), cache_dir)
    return _MODEL

@signals.worker_process_init.connect
def _prewarm_sbert(**kwargs):
    _ = get_sbert_model()  # ensure load happens at child start, not first task
