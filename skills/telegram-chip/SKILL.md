---
name: telegram-chip
description: >
  Unified Telegram **user-account** core (Telethon HTTP API + helpers) for an
  agent that needs MTProto access. Use only when you actually need a user
  account (read history, write to chats where bots cannot, search messages,
  export chats). For ordinary bot integrations, do NOT use this — use the
  Telegram Bot API (`api.telegram.org/bot<token>/...`) directly.
---

# telegram-chip

## ⚠ Read this before installing

This skill runs a **Telegram user account** via [Telethon](https://github.com/LonamiWebs/Telethon)
MTProto. That is a strictly different beast from the Telegram Bot API:

- It logs in as a **person**, not a bot.
- Telegram MTProto requires that **one session string = one running process**.
  Multiple concurrent processes with the same session string trigger
  `AuthKeyDuplicatedError` and may get the session revoked.
- The session string is equivalent to your password. Treat it like one.
- Most agent automation needs the **Bot API**, not this. If a bot can do
  what you want, use a bot. Only use this skill if you specifically need to
  act as a real user (e.g. read non-bot chats, export history, search across
  all dialogs).

If you only need bot-API access, **skip this skill entirely**.

## What it provides

- One Telethon client running in a single process, exposed over a local HTTP
  API (`api.py`, FastAPI). Other agents on the same host talk to it via
  `http://127.0.0.1:8080`.
- Helpers for: read/send messages, list chats, read poll results, export
  history, check read-receipts (`read_outbox_max_id`).
- Dockerfile and `docker-compose.yml` for isolated runtime.

## Authorization procedure (do exactly in this order)

1. Get `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` at `https://my.telegram.org/apps`.
   - Prefer phone mobile internet (no Wi-Fi/VPN), **or** a clean
     private/incognito browser session. This reduces auth friction.
2. `cp .env.example .env` and fill in API ID + hash.
3. Run `./generate-session.sh` and complete interactive login:
   - phone number,
   - Telegram login code from the SMS / Telegram app,
   - 2FA password (if enabled).
4. Verify `TELEGRAM_SESSION_STRING` is present in `.env`.
5. Start exactly **one** active Telethon process bound to that session.

## Hard rules

- Never share `TELEGRAM_SESSION_STRING` in chat, logs, or commits. The
  `.gitignore` excludes `.env` already — keep it that way.
- If the session leaks, revoke it in Telegram → Settings → Devices, then
  re-run `./generate-session.sh`.
- Exactly one Telethon process per session string. Period.
- Talk to this skill **only** through its HTTP API (`api.py`). Do not
  import Telethon from outside this skill's virtualenv.

## Reading polls

Anonymous multi-choice polls hide per-option vote counts from non-voters.
You must vote manually in Telegram first (as the session owner). After that,
`GET /chats/{chat_id}/messages/{message_id}` returns
`poll.results[].voters`. See `README.md` → "Reading polls".

## Read receipts (`read_outbox_max_id`)

To check whether a peer has read a message you sent:
`sent_message_id <= read_outbox_max_id` ⇒ read.

- `GET /chats/list` returns `read_outbox_max_id`, `read_inbox_max_id`,
  `top_message_id` per chat alongside `unread_count`.
- `GET /chats/{chat_id}/read_status` — single-peer endpoint, lighter
  than `/chats/list` (uses `GetPeerDialogsRequest`).

`read_outbox_max_id == top_message_id` ⇒ the peer has read everything.

See `README.md` and `TROUBLESHOOTING.md` for full setup, endpoint reference,
and failure recovery.
