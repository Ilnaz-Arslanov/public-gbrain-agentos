"""FastEmbed wrapper with LRU cache."""
import hashlib
import logging
from collections import OrderedDict

from fastembed import TextEmbedding

logger = logging.getLogger(__name__)

# Peak RSS is dominated by the ONNX arena the batch allocates, not by the
# model itself. Measured on prod (e5-large, fastembed 0.8.0): the model
# alone sits at ~1.5 GB, a batch of 32 peaks at 3.1 GB, a batch of 8 at
# 1.9 GB. The unit caps the worker at 2560M, so 32 put every document
# longer than one batch into an OOM restart loop -- one 117 KB note stalled
# the whole queue for eight days. 8 keeps a ~640 MB headroom.
BATCH_SIZE = 8
CACHE_MAX_SIZE = 512


def _cache_key(text: str) -> str:
    """Compute short sha256 key for cache lookup."""
    return hashlib.sha256(text.encode()).hexdigest()[:32]


class Embedder:
    """Batch text embedder backed by FastEmbed with in-memory cache.

    Model is loaded lazily on first call to embed() or embed_single().
    Cache uses OrderedDict as LRU with maxsize check.
    """

    def __init__(self, model_name: str = "intfloat/multilingual-e5-large") -> None:
        self._model_name = model_name
        self._model: TextEmbedding | None = None
        self._cache: OrderedDict[str, list[float]] = OrderedDict()

    def _ensure_model(self) -> TextEmbedding:
        """Load model on first use."""
        if self._model is None:
            logger.info("Loading FastEmbed model: %s", self._model_name)
            self._model = TextEmbedding(model_name=self._model_name)
            logger.info("FastEmbed model loaded")
        return self._model

    def _cache_get(self, text: str) -> list[float] | None:
        """Get cached embedding, updating LRU order."""
        key = _cache_key(text)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def _cache_put(self, text: str, embedding: list[float]) -> None:
        """Store embedding in cache, evicting oldest if full."""
        key = _cache_key(text)
        self._cache[key] = embedding
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX_SIZE:
            self._cache.popitem(last=False)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch embed texts, using cache where possible.

        Args:
            texts: List of strings to embed.

        Returns:
            List of embedding vectors in same order as input.
        """
        model = self._ensure_model()

        results: list[list[float] | None] = [None] * len(texts)
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        for i, text in enumerate(texts):
            cached = self._cache_get(text)
            if cached is not None:
                results[i] = cached
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)

        if uncached_texts:
            all_embeddings: list[list[float]] = []
            for batch_start in range(0, len(uncached_texts), BATCH_SIZE):
                batch = uncached_texts[batch_start:batch_start + BATCH_SIZE]
                batch_results = list(model.embed(batch))
                for vec in batch_results:
                    all_embeddings.append(vec.tolist())

            for i, (idx, emb) in enumerate(
                zip(uncached_indices, all_embeddings)
            ):
                results[idx] = emb
                self._cache_put(uncached_texts[i], emb)

        return [r for r in results if r is not None]

    def embed_single(self, text: str) -> list[float]:
        """Embed a single text string.

        Args:
            text: Text to embed.

        Returns:
            Embedding vector.
        """
        return self.embed([text])[0]
