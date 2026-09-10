"""CAP-5 — local embeddings (BR-33, NFR-10).

The model runs inside the boundary. Source code and wiki text are never
sent anywhere to be indexed; only a user's final question reaches a hosted
model at answer time (M4). That split is the point, so it is recorded in
the embedding_model table on every run and reported by `eck status`.
"""
from __future__ import annotations

import struct
from typing import Iterable, Sequence

# 384 dims, ~130MB, strong on short semantic matches and cheap on CPU.
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

# bge-* v1.5 models are trained with an asymmetric query prefix. Passages
# are embedded bare; queries get this prefix. Omitting it measurably hurts.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class EmbeddingUnavailable(RuntimeError):
    """Raised when no local embedder can be constructed.

    Deliberately fatal: a keyword-only fallback would silently violate
    BR-33 ('retrieve by meaning, not only exact words') while still
    appearing to work.
    """


class LocalEmbedder:
    def __init__(self, model_name: str = DEFAULT_MODEL):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "sentence-transformers is not installed; run "
                "`pip install -r requirements.txt`") from exc
        self.name = model_name
        self.model = SentenceTransformer(model_name)
        self.dim = self.model.get_sentence_embedding_dimension()
        self.local = True

    def encode(self, texts: Sequence[str], batch_size: int = 64,
               progress: bool = False):
        return self.model.encode(
            list(texts), batch_size=batch_size, convert_to_numpy=True,
            normalize_embeddings=True,       # cosine becomes a dot product
            show_progress_bar=progress)

    def encode_query(self, text: str):
        return self.encode([QUERY_PREFIX + text])[0]


def pack(vec: Iterable[float]) -> bytes:
    vals = list(vec)
    return struct.pack(f"<{len(vals)}f", *vals)


def unpack(blob: bytes, dim: int) -> tuple:
    return struct.unpack(f"<{dim}f", blob)
