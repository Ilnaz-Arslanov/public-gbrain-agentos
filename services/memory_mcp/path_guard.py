"""Path safety guard for vault filesystem operations."""
from pathlib import Path

ALLOWED_SCOPES = frozenset({
    "10-strategy",
    "10-system",
    "20-daily",
    "20-metrics",
    "30-decisions",
    "40-projects",
    "50-external",
    "50-knowledge",
    "50-ops",
    "60-tasks",
    "70-runbooks",
    "80-error-patterns",
    "90-inbox",
    "_templates",
})


def normalize_rel_path(path: str) -> str:
    """Reject non-canonical spellings of a vault-relative path.

    Guards that classify a path by its segments (scope, owning agent,
    canonical kind) must see the same segments the filesystem will. A
    spelling like ``50-ops/ivi/./30-decisions/note.md`` resolves to a real
    file but classifies as kind ``.`` -- enough to walk past an immutability
    or ownership check. Rather than normalize silently, refuse: every caller
    writes plain paths anyway, so a redundant segment means either a bug or
    an attempt.

    Args:
        path: Relative path within the vault.

    Returns:
        The path unchanged, once every segment is proven meaningful.

    Raises:
        ValueError: Path is empty, absolute, or carries ``..``, ``~``,
            ``.`` or empty segments.
    """
    if not path:
        raise ValueError("Path must not be empty")

    if "~" in path:
        raise ValueError(f"Home expansion blocked: '~' in '{path}'")

    if path.startswith("/"):
        raise ValueError(f"Absolute paths blocked: '{path}'")

    segments = path.split("/")
    if ".." in segments:
        raise ValueError(f"Path traversal blocked: '..' in '{path}'")
    if "" in segments:
        raise ValueError(f"Empty path segment in '{path}'")
    if "." in segments:
        raise ValueError(f"Redundant '.' segment in '{path}'")

    return path


def validate_path(path: str, vault_root: str) -> Path:
    """Validate and resolve a vault-relative path.

    Args:
        path: Relative path within the vault (e.g. '30-decisions/my-note.md').
        vault_root: Absolute path to the vault root directory.

    Returns:
        Resolved absolute Path within the vault.

    Raises:
        ValueError: If the path is unsafe or targets an unknown scope.
    """
    path = normalize_rel_path(path)

    # Extract top-level scope from the path
    top_level = path.split("/")[0]
    if top_level not in ALLOWED_SCOPES:
        raise ValueError(
            f"Unknown scope '{top_level}'. "
            f"Allowed: {', '.join(sorted(ALLOWED_SCOPES))}"
        )

    root = Path(vault_root).resolve()
    resolved = (root / path).resolve()

    # Final containment check -- resolved path must be under vault_root
    if not str(resolved).startswith(str(root) + "/") and resolved != root:
        raise ValueError(
            f"Path '{path}' resolves outside vault root"
        )

    return resolved
