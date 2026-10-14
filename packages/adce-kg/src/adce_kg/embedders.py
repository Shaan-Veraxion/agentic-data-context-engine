"""Embedders for the Graphiti knowledge service.

- vertex: Gemini embeddings on Google Cloud Vertex AI (graphiti's GeminiEmbedder with a Vertex genai client).
- hash:   deterministic feature hashing of words and character trigrams. Offline and reproducible, useful for tests
          and local work, but it is NOT semantic search. Which model to use in production is open question Q-16.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable

from adce_core.settings import Settings
from graphiti_core.embedder.client import EmbedderClient


class HashEmbedder(EmbedderClient):
    def __init__(self, dim: int = 768) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        t = text.lower()
        feats = re.findall(r"\w+", t) + [t[i:i + 3] for i in range(max(0, len(t) - 2))]
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    async def create(self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]) -> list[float]:
        if isinstance(input_data, str):
            return self._vec(input_data)
        items = list(input_data)  # type: ignore[arg-type]
        return self._vec(" ".join(str(x) for x in items))

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in input_data_list]


def make_embedder(settings: Settings) -> EmbedderClient:
    if settings.embedder == "hash":
        return HashEmbedder(settings.embedding_dim)
    if not settings.vertex_project:
        raise ValueError("ADCE_EMBEDDER=vertex needs ADCE_VERTEX_PROJECT")
    from google import genai
    from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig

    client = genai.Client(vertexai=True, project=settings.vertex_project, location=settings.vertex_region)
    return GeminiEmbedder(config=GeminiEmbedderConfig(embedding_model=settings.embedding_model,
                                                      embedding_dim=settings.embedding_dim), client=client)
