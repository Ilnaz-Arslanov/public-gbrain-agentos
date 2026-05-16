---
name: instagram-superpower
description: >
  Instagram analytics via HikerAPI: top reels per account, watchlist of accounts
  to scan periodically, engagement scoring. Use when: (1) you need to analyze
  an Instagram account (followers, posts, top reels), (2) build a watchlist of
  competitors, (3) find the most-engaging reels of an account over a window of
  N days.
  Triggers: «analyze instagram account», «top reels», «watchlist», «engagement»,
  Instagram profile URLs.
  NOT for: YouTube, Twitter, mass spam, auto-likes, auto-follows.
---

# instagram-superpower

Analytics for Instagram accounts using [HikerAPI](https://hikerapi.com) SaaS
(public API, paid per request).

The **download** half of this skill (media files from Instagram CDN via a
self-hosted Cobalt instance) was tightly coupled to a private VPS layout in
the original codebase. It has been removed from this public distro. If you
want it back, deploy your own [Cobalt](https://github.com/imputnet/cobalt) and
wire its URL into your own script.

## Setup

```sh
mkdir -p ~/.secrets/hikerapi
echo "<YOUR_HIKERAPI_KEY>" > ~/.secrets/hikerapi/api-key
chmod 600 ~/.secrets/hikerapi/api-key
```

## Usage

### Analyze a single account

```sh
bash scripts/analyze.sh <username> [top_N] [days]
# Example:
bash scripts/analyze.sh someuser 5 14
```

Output: profile stats + top-N reels by engagement (views + likes*10) for the
last N days, both human-readable and JSON.

### Watchlist

```sh
bash scripts/watchlist.sh list
bash scripts/watchlist.sh add <username>
bash scripts/watchlist.sh remove <username>
bash scripts/watchlist.sh scan [top_N] [days]
```

`scan` walks every account in `~/.secrets/cobalt/watchlist.json` and calls
`analyze.sh` on each with a 10-second pause between requests.

## API key & balance

Each request costs ~$0.001. Check balance at the end of `analyze.sh` output
or via `curl -H "x-access-key: $KEY" https://api.instagrapi.com/sys/balance`.

## Omitted scripts

- `scripts/download.sh` — required a private Cobalt VPS.
- `scripts/check-cookies.sh` — same.
- `scripts/deploy-cookies.sh` — same.
- `references/cookie-refresh.md` — credential-rotation runbook for a private setup.

These are kept as stubs that exit 1, so existing references do not crash; replace
with your own implementation if you need media download.
