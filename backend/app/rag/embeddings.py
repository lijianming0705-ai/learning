from __future__ import annotations

import hashlib
import math
from collections import Counter
from typing import Protocol

import httpx
import numpy as np

from app.config import OLLAMA_URL
from app.rag.tokenizer import extract_hybrid_tokens


class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, text: str) -> np.ndarray: ...


class LocalMultilingualEmbedder:
    """Deterministic 512-dim L2-normalized feature-hashing embedder.

    Uses signed projections over CJK unigrams/bigrams/trigrams + Latin words/subwords
    with sublinear TF weighting (1 + log(tf)) so semantic overlap works offline for both
    Chinese and English without requiring external model downloads.
    """

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim
        self.name = "hybrid-local"

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        tokens = extract_hybrid_tokens(text, include_trigrams=True)
        if not tokens:
            return vec

        counts = Counter(tokens)
        for token, tf in counts.items():
            # Bigrams and multi-char words carry stronger semantic signal than single CJK chars
            length_boost = 1.6 if (len(token) >= 2 and not token.startswith("~")) else 1.0
            weight = (1.0 + math.log(tf)) * length_boost
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            raw = int.from_bytes(digest, "little", signed=False)
            bucket = raw % self.dim
            sign = 1.0 if ((raw >> 63) == 0) else -1.0
            vec[bucket] += sign * weight

        norm = float(np.linalg.norm(vec))
        if norm > 1e-12:
            vec /= norm
        return vec


class OllamaEmbedder:
    """Optional Ollama embeddings adapter with graceful fallback to local embedder."""

    def __init__(self, model: str, fallback: LocalMultilingualEmbedder | None = None) -> None:
        self.model = model
        self.fallback = fallback or LocalMultilingualEmbedder()
        self.dim = self.fallback.dim
        self.name = f"ollama:{model}"

    def embed(self, text: str) -> np.ndarray:
        if not self.model or not text.strip():
            return self.fallback.embed(text)
        try:
            resp = httpx.post(
                f"{OLLAMA_URL.rstrip('/')}/api/embeddings",
                json={"model": self.model, "prompt": text[:4000]},
                timeout=8.0,
            )
            resp.raise_for_status()
            data = resp.json()
            emb = data.get("embedding")
            if isinstance(emb, list) and emb:
                arr = np.asarray(emb, dtype=np.float32)
                norm = float(np.linalg.norm(arr))
                if norm > 1e-12:
                    arr /= norm
                self.dim = int(arr.shape[0])
                return arr
        except Exception:
            pass
        return self.fallback.embed(text)
