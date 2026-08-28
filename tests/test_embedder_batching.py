"""Batching guards for the ingest worker embedder.

The queue once stalled for eight days because one long note produced a
batch big enough to push the worker past its 2560M unit cap. These tests
pin the two properties that kept it alive: batches stay small, and the
cache does not silently drop them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ingest_worker import embedder as em  # noqa: E402

# Peak RSS measured on prod with e5-large/fastembed 0.8.0: model ~1.5 GB,
# batch of 8 peaks at 1.9 GB, batch of 32 at 3.1 GB against a 2560M cap.
MAX_SAFE_BATCH = 8


class _FakeModel:
    """Records the size of every batch handed to the real model."""

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        batch = list(texts)
        self.batch_sizes.append(len(batch))
        # The real model yields numpy vectors; the embedder calls .tolist().
        return [np.array([float(len(t))]) for t in batch]


@pytest.fixture
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> tuple[em.Embedder, _FakeModel]:
    model = _FakeModel()
    inst = em.Embedder()
    monkeypatch.setattr(inst, "_ensure_model", lambda: model)
    return inst, model


def test_batch_size_stays_within_memory_cap() -> None:
    """A larger batch is what put the worker into an OOM restart loop."""
    assert em.BATCH_SIZE <= MAX_SAFE_BATCH


def test_long_document_is_split_into_bounded_batches(
    fake_embedder: tuple[em.Embedder, _FakeModel],
) -> None:
    inst, model = fake_embedder
    texts = [f"chunk {i}" for i in range(em.BATCH_SIZE * 3 + 1)]

    result = inst.embed(texts)

    assert len(result) == len(texts)
    assert model.batch_sizes  # the model was actually called
    assert max(model.batch_sizes) <= em.BATCH_SIZE
    assert sum(model.batch_sizes) == len(texts)


def test_repeated_text_is_served_from_cache(
    fake_embedder: tuple[em.Embedder, _FakeModel],
) -> None:
    inst, model = fake_embedder
    inst.embed(["same", "other"])
    calls_after_first = sum(model.batch_sizes)

    again = inst.embed(["same", "other"])

    assert sum(model.batch_sizes) == calls_after_first
    assert again == [[4.0], [5.0]]
