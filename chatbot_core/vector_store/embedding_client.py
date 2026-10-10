from typing import List
from .embeddings import get_sbert_model
from .config import policy
from .faiss_index import unit_vector

def get_embedding(text: str) -> List[float]:
    """Inline CPU encoding; validates vectors before persistence/search."""
    model = get_sbert_model()
    return unit_vector(model.encode([text])[0], policy().dimension).tolist()
