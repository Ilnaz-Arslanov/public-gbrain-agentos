#!/usr/bin/env python3
"""
inbox-agent Telegram bot daemon.

Long-polling Telegram bot that ACKs every forward immediately and pipes the
message through `hooks/save-to-raw.sh` (which dual-writes to local raw/ AND to
the shared gbrain via memory-mcp).

This closes the «I deployed it, I forwarded a YouTube link, nothing happened»
UX cliff: every forward gets a synchronous ack and a final status reply.

Env vars (read from os.environ; install-local.sh writes them to
$INBOX_AGENT_HOME/.env):
    INBOX_AGENT_HOME    e.g. ~/.claude-lab/inbox-agent
    PRINCE_CHAT_ID      numeric Telegram user id (only this id may forward).
                        Set to "0" to disable allowlist (open to anyone — not
                        recommended).
    TG_BOT_TOKEN_FILE   default: $INBOX_AGENT_HOME/secrets/telegram-bot-token

Run as `python3 bot.py` or via the bundled systemd unit template.

Requires: python-telegram-bot>=20.0 (see requirements.txt).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("inbox-bot")

URL_RE = re.compile(r"https?://[^\s]+")

INBOX_AGENT_HOME = Path(
    os.environ.get("INBOX_AGENT_HOME", Path.home() / ".claude-lab/inbox-agent")
)
HOOK = INBOX_AGENT_HOME / "hooks" / "save-to-raw.sh"
RAW_DIR = INBOX_AGENT_HOME / "raw"
COMPILED_DIR = INBOX_AGENT_HOME / "compiled"
TOKEN_FILE = Path(
    os.environ.get(
        "TG_BOT_TOKEN_FILE",
        str(INBOX_AGENT_HOME / "secrets" / "telegram-bot-token"),
    )
)
ALLOWLIST_RAW = os.environ.get("PRINCE_CHAT_ID", "0").strip()
ALLOWLIST = {int(ALLOWLIST_RAW)} if ALLOWLIST_RAW not in ("", "0") else set()


def _read_token() -> str:
    if not TOKEN_FILE.exists():
        raise SystemExit(f"telegram bot token file not found: {TOKEN_FILE}")
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def _allowed(user_id: int | None) -> bool:
    if not ALLOWLIST:
        return True
    return user_id in ALLOWLIST


async def _run_hook(user_text: str, source_tag: str) -> tuple[int, str, str]:
    """Call save-to-raw.sh, return (rc, stdout, stderr_last_line)."""
    if not HOOK.exists():
        return 127, "", f"hook not found at {HOOK}"

    proc = await asyncio.create_subprocess_exec(
        "bash",
        str(HOOK),
        user_text,
        "",  # agent_response (empty — bot doesn't have one)
        source_tag,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "INBOX_AGENT_HOME": str(INBOX_AGENT_HOME)},
    )
    out_b, err_b = await proc.communicate()
    out = out_b.decode("utf-8", errors="replace").strip()
    err = err_b.decode("utf-8", errors="replace").strip()
    err_last = err.splitlines()[-1] if err else ""
    return proc.returncode or 0, out, err_last


def _classify(text: str) -> tuple[str, str | None]:
    """Cheap mirror of save-to-raw.sh classifier — for the ack reply only."""
    m = URL_RE.search(text)
    url = m.group(0) if m else None
    if not url:
        return "text", None
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube", url
    if "instagram.com" in url:
        return "instagram", url
    if "twitter.com" in url or "x.com" in url:
        return "twitter", url
    if "reddit.com" in url:
        return "web", url
    return "web", url


def _slug_from_url(url: str) -> str:
    s = re.sub(r"^https?://", "", url)
    s = re.sub(r"^www\.", "", s)
    s = re.sub(r"[^a-zA-Z0-9]", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:60]


async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _allowed(user.id if user else None):
        return
    msg = (
        "I'm your inbox-agent. Forward links or text here and I'll archive them "
        "to your local raw store and to your shared knowledge base.\n\n"
        "Commands:\n"
        "/status — how many items I've stored\n"
        "/start — this message"
    )
    await update.effective_message.reply_text(msg)


async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _allowed(user.id if user else None):
        return

    by_month: dict[str, int] = {}
    if RAW_DIR.exists():
        for p in RAW_DIR.rglob("*.md"):
            # raw/YYYY-MM/<type>/<slug>.md — second-from-root component
            parts = p.relative_to(RAW_DIR).parts
            month = parts[0] if parts else "unknown"
            by_month[month] = by_month.get(month, 0) + 1

    last_titles: list[str] = []
    if COMPILED_DIR.exists():
        files = sorted(
            COMPILED_DIR.rglob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:3]
        for f in files:
            last_titles.append(f.stem)

    lines = ["<b>inbox-agent status</b>", ""]
    if by_month:
        lines.append("Items by month:")
        for month in sorted(by_month):
            lines.append(f"  {month}: <b>{by_month[month]}</b>")
    else:
        lines.append("No items yet.")
    if last_titles:
        lines.append("")
        lines.append("Last 3 compiled:")
        for t in last_titles:
            lines.append(f"  - {t}")
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def on_message(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None:
        return
    if not _allowed(user.id if user else None):
        log.warning("rejected message from uid=%s", user.id if user else None)
        return

    text = msg.text or msg.caption or ""
    if not text.strip():
        await msg.reply_text("Empty message — nothing to save.")
        return

    ack = await msg.reply_text("Got it, processing...")

    type_hint, _url = _classify(text)
    rc, _out, err_last = await _run_hook(text, source_tag="telegram")

    if rc == 0:
        m = URL_RE.search(text)
        slug = _slug_from_url(m.group(0)) if m else f"text-{int(datetime.now(timezone.utc).timestamp())}"
        reply = f"Saved to knowledge base. Type: {type_hint}, slug: {slug}"
    else:
        snippet = (err_last or "unknown error")[:200]
        reply = f"Error (rc={rc}): {snippet}"

    try:
        await ack.edit_text(reply)
    except Exception:
        await msg.reply_text(reply)


def main() -> None:
    token = _read_token()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_message)
    )
    app.add_handler(MessageHandler(filters.CAPTION, on_message))
    log.info(
        "inbox-bot starting (allowlist=%s, hook=%s)",
        ALLOWLIST or "open",
        HOOK,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
