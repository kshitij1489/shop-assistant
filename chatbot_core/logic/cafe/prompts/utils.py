import re, json, hashlib
from rapidfuzz.fuzz import token_set_ratio

def normalize(t: str) -> str:
    t = (t or "").strip().lower()
    return re.sub(r"\s+", " ", t)

def kb_fingerprint(obj) -> str:
    try:
        payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        payload = str(obj)
    return hashlib.sha256(payload.encode()).hexdigest()

def exact_sig(system_id: str, model: str, scope: str, kb_fp: str, question: str) -> str:
    h = hashlib.sha256()
    h.update(system_id.encode()); h.update(model.encode())
    h.update(scope.encode());     h.update(kb_fp.encode())
    h.update(normalize(question).encode())
    return h.hexdigest()
