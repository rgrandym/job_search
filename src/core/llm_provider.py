"""The single boundary between this codebase and any LLM / embedding backend.

Rules (see CLAUDE.md):
- Only this module and `src/core/llm/` talk to model APIs. Everything else depends on the
  `LLMProvider` / `Embedder` protocols defined here (backends live in `src/core/llm/`).
- Every LLM call returns a validated Pydantic model. Free-text responses are not
  part of the contract.
- Tests inject fakes that satisfy the protocols. They never hit the network.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from src.core.config import get_settings
from src.core.llm.types import LLMConfig, LLMError, Role

__all__ = ["LLMError", "LLMProvider", "Embedder", "HashingEmbedder", "cosine"]

T = TypeVar("T", bound=BaseModel)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")


@runtime_checkable
class LLMProvider(Protocol):
    """Structured-output text generation."""

    def generate(self, *, system: str, prompt: str, output_model: type[T]) -> T:
        """Run one completion and return it parsed into `output_model`."""
        ...


@runtime_checkable
class Embedder(Protocol):
    """Text -> dense vector. Vectors must be L2-normalised."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts."""
        ...


class HashingEmbedder:
    """Dependency-free, deterministic embedder (feature hashing of unigrams + bigrams).

    Good enough for lexical-semantic retrieval and fully reproducible in tests.
    Swap in a neural embedder (e.g. Voyage, sentence-transformers) by implementing
    `Embedder` in this module. Callers are unaffected.
    """

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim or get_settings().embedding_dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed each text as an L2-normalised hashed bag of n-grams."""
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = _TOKEN_RE.findall(text.lower())
        grams = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:], strict=False)]
        for gram in grams:
            digest = hashlib.blake2b(gram.encode(), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def get_llm_provider(role: Role = "screening", config: LLMConfig | None = None) -> LLMProvider:
    """Structured-output provider for the configured backend. Tests pass fakes instead."""
    from src.core.llm import make_structured

    return make_structured(config or LLMConfig.from_settings(get_settings()), role)


def get_embedder() -> Embedder:
    """Factory for the configured embedder."""
    return HashingEmbedder()
