"""Document change detection via content hashing."""
import hashlib

import asyncpg

HASH_PREFIX_LEN = 16


def compute_hash(text: str) -> str:
    """Compute truncated SHA-256 hash of document text.

    Args:
        text: Document body.

    Returns:
        First 16 hex characters of SHA-256 digest.
    """
    return hashlib.sha256(text.encode()).hexdigest()[:HASH_PREFIX_LEN]


async def check_changed(
    pool: asyncpg.Pool, doc_id: int, new_hash: str
) -> bool:
    """Compare new hash with stored document hash.

    Args:
        pool: Asyncpg connection pool.
        doc_id: Document ID in the documents table.
        new_hash: Hash of the current document body.

    Returns:
        True if document changed or not found, False if unchanged.
    """
    row = await pool.fetchrow(
        "SELECT sha256 FROM documents WHERE id = $1", doc_id
    )
    if row is None:
        return True
    return row["sha256"] != new_hash
