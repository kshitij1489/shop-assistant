import os, threading, faiss, numpy as np
from django.conf import settings
from chatbot_core.models import FaissVector

DIM = 384
INDEX_PATH = os.getenv("FAISS_INDEX_PATH", "/var/lib/cafe/faiss.index")

_lock = threading.Lock()
_index = faiss.IndexFlatIP(DIM)   # cosine via normalized vectors
_ids: list[int] = []              # position -> DB primary key

def load_from_vectors(vectors: np.ndarray, ids: list[int], dim: int = DIM):
    global _index, _ids
    with _lock:
        _ids = ids[:]
        if vectors.size == 0:
            _index = faiss.IndexFlatIP(dim)
            return
        if vectors.shape[1] != dim:
            raise ValueError(f"Vector dim mismatch: {vectors.shape[1]} vs {dim}")
        _index = faiss.IndexFlatIP(dim)
        _index.add(vectors.astype("float32"))

def add_vector(vec: np.ndarray, db_id: int):
    v = vec.astype("float32")[None, ...]
    with _lock:
        _index.add(v)
        _ids.append(db_id)

def search(vec: np.ndarray, k: int):
    if _index.ntotal == 0:
        return [], []
    q = vec.astype("float32")[None, ...]
    with _lock:
        D, I = _index.search(q, k)
    ids = [ _ids[i] for i in I[0] if i != -1 ]
    sims = [ float(s) for s in D[0][:len(ids)] ]
    return ids, sims

def rebuild_faiss_from_db():
    rows = FaissVector.objects.select_related("cache_entry").only("cache_entry_id","vector","dim")
    ids, vecs = [], []
    for r in rows.iterator():
        if r.dim != DIM:  # skip incompatible entries
            continue
        ids.append(r.cache_entry_id)
        vecs.append(np.frombuffer(r.vector, dtype="float32"))
    V = np.vstack(vecs) if vecs else np.zeros((0, DIM), dtype="float32")
    load_from_vectors(V, ids, dim=DIM)