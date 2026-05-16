#!/usr/bin/env python3
"""issue-agent-token.py — generate a Bearer token and register it in `agent_tokens`.

Usage:
    issue-agent-token.py --agent <name> --scopes 'read,write,80-error-patterns'
    issue-agent-token.py --agent admin --scopes '*'
    issue-agent-token.py --agent <name> --revoke
    issue-agent-token.py --agent <name> --revoke-id <token_sha256_prefix>

Behaviour:
    1. Generate a 256-bit random token (base64url, ~43 chars).
    2. Compute sha256(token).
    3. INSERT into `agent_tokens (agent, token_sha256, can_write_scopes, can_read_scopes)`.
    4. Print the raw token ONCE to stdout. It cannot be recovered later.

Database credentials are read from environment (preferred) or `.env` in the repo root:
    PG_DATABASE, PG_USER, PG_HOST (default /var/run/postgresql), PG_PORT (default 5432),
    PG_PASSWORD (optional — required for TCP host auth, ignored on peer auth via socket).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import secrets
import sys
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Tiny .env loader. Skips comments and blank lines."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        os.environ.setdefault(k, v)


def parse_scopes(s: str) -> list[str]:
    """Parse a comma-separated scope list. '*' means wildcard (single-element list)."""
    if s.strip() == "*":
        return ["*"]
    return [item.strip() for item in s.split(",") if item.strip()]


def _resolve_db_kwargs() -> dict[str, str | int]:
    """Build asyncpg connect kwargs from env. Mirrors services/shared/config.py logic."""
    db_name = os.environ.get("PG_DATABASE", "gbrain")
    db_user = os.environ.get("PG_USER", "gbrain")
    db_host = os.environ.get("PG_HOST", "/var/run/postgresql")
    db_port = int(os.environ.get("PG_PORT", "5432"))
    db_password = os.environ.get("PG_PASSWORD", "")

    kwargs: dict[str, str | int] = {
        "database": db_name,
        "user": db_user,
        "host": db_host,
        "port": db_port,
    }
    # asyncpg silently ignores password on peer-auth unix sockets, so passing
    # it unconditionally when set is safe.
    if db_password:
        kwargs["password"] = db_password
    return kwargs


async def _issue(agent: str, write_scopes: list[str], read_scopes: list[str]) -> str:
    import asyncpg  # imported lazily so --help works without the venv

    token = secrets.token_urlsafe(32)  # 256-bit
    token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()

    conn = await asyncpg.connect(**_resolve_db_kwargs())
    try:
        await conn.execute(
            """
            INSERT INTO agent_tokens (agent, token_sha256, can_write_scopes, can_read_scopes)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (agent) DO UPDATE
              SET token_sha256 = EXCLUDED.token_sha256,
                  can_write_scopes = EXCLUDED.can_write_scopes,
                  can_read_scopes = EXCLUDED.can_read_scopes,
                  last_rotated = now(),
                  revoked_at = NULL
            """,
            agent, token_sha256, write_scopes, read_scopes,
        )
    finally:
        await conn.close()

    print(
        f"# agent={agent} sha256={token_sha256[:12]}... "
        f"write_scopes={write_scopes} read_scopes={read_scopes}",
        file=sys.stderr,
    )
    return token


async def _revoke(agent: str, revoke_id: str | None) -> int:
    """Revoke tokens for an agent. Returns count of rows affected."""
    import asyncpg

    conn = await asyncpg.connect(**_resolve_db_kwargs())
    try:
        if revoke_id:
            # Match by sha256 prefix (first N chars). User passes the short id
            # shown on issue (e.g. first 12 chars). Match `token_sha256 LIKE 'prefix%'`.
            result = await conn.execute(
                """
                UPDATE agent_tokens
                SET revoked_at = now()
                WHERE agent = $1 AND token_sha256 LIKE $2 AND revoked_at IS NULL
                """,
                agent, f"{revoke_id}%",
            )
        else:
            result = await conn.execute(
                """
                UPDATE agent_tokens
                SET revoked_at = now()
                WHERE agent = $1 AND revoked_at IS NULL
                """,
                agent,
            )
        # asyncpg returns 'UPDATE N' where N is row count.
        try:
            count = int(result.split()[-1])
        except (ValueError, IndexError):
            count = 0
        return count
    finally:
        await conn.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent", required=True, help="agent name (unique identifier)")
    parser.add_argument(
        "--scopes",
        default=None,
        help="comma-separated scopes (e.g. 'read,write,80-error-patterns') or '*' for admin",
    )
    parser.add_argument(
        "--read-scopes",
        default=None,
        help="separate read scopes (defaults to same as --scopes)",
    )
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="revoke all active tokens for the agent (sets revoked_at = now())",
    )
    parser.add_argument(
        "--revoke-id",
        default=None,
        help="revoke a specific token by sha256 prefix (e.g. first 12 chars)",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")

    try:
        import asyncpg  # noqa: F401 - presence check
    except ImportError:
        print(
            "ERROR: asyncpg not installed. Activate the venv first: "
            "source ${INSTALL_DIR}/.venv/bin/activate",
            file=sys.stderr,
        )
        return 2

    if args.revoke or args.revoke_id:
        n = asyncio.run(_revoke(args.agent, args.revoke_id))
        print(f"# revoked {n} token(s) for agent={args.agent}", file=sys.stderr)
        return 0 if n > 0 else 1

    if not args.scopes:
        parser.error("either --scopes or --revoke/--revoke-id is required")

    write_scopes = parse_scopes(args.scopes)
    read_scopes = parse_scopes(args.read_scopes) if args.read_scopes else write_scopes

    token = asyncio.run(_issue(args.agent, write_scopes, read_scopes))

    # Print the raw token ONCE. Caller is responsible for capturing it.
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
