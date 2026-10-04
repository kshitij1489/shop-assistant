from typing import List
import numpy as np
from .embeddings import get_sbert_model

def get_embedding(text: str, timeout: float = 5.0) -> List[float]:
    """
    Simple, cycle-safe: compute inline using the shared SentenceTransformer.
    If you later want to offload to Celery for web requests only, we can add that back safely.
    """
    model = get_sbert_model()
    vec = model.encode([text])[0].astype("float32")
    n = np.linalg.norm(vec)
    if n > 0:
        vec = vec / n
    return vec.tolist()
