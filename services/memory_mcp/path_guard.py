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
    "60-tasks",
    "70-runbooks",
    "80-error-patterns",
    "90-inbox",
    "_templates",
})


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
    if not path:
        raise ValueError("Path must not be empty")

    if ".." in path:
        raise ValueError(f"Path traversal blocked: '..' in '{path}'")

    if "~" in path:
        raise ValueError(f"Home expansion blocked: '~' in '{path}'")

    if path.startswith("/"):
        raise ValueError(f"Absolute paths blocked: '{path}'")

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
