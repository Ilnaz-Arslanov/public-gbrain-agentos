"""Sliding window text chunker for document ingestion."""


WINDOW_SIZE_DEFAULT = 500
OVERLAP_DEFAULT = 50


def chunk_text(
    text: str,
    window_size: int = WINDOW_SIZE_DEFAULT,
    overlap: int = OVERLAP_DEFAULT,
) -> list[str]:
    """Split text into overlapping chunks by word count.

    Args:
        text: Source text to chunk.
        window_size: Number of words per chunk.
        overlap: Number of overlapping words between adjacent chunks.

    Returns:
        List of chunk strings. Single-element list if text is shorter
        than window_size words.
    """
    words = text.split()
    if not words:
        return []

    if len(words) <= window_size:
        return [" ".join(words)]

    step = window_size - overlap
    chunks: list[str] = []
    start = 0

    while start < len(words):
        end = start + window_size
        chunk_words = words[start:end]
        chunks.append(" ".join(chunk_words))

        if end >= len(words):
            break
        start += step

    return chunks
