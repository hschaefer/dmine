# dmine

Bot-free Discord channel archiver & data miner. It drives a normal logged-in
browser (Playwright + system Chrome) and reads whatever the account can see,
exactly like a human scrolling. No bot token, no API key. Output: one shared
SQLite archive plus the downloaded media files.

> **Unofficial and unaffiliated.** This project is not affiliated with,
> endorsed by, or supported by Discord Inc. "Discord" is a trademark of
> Discord Inc. and is used here only to describe what the tool interacts with.
>
> **Read this before using it.** dmine automates a *user account*. That
> is not what Discord's Terms of Service and API rules permit for bots — the
> supported path for programmatic access is the official API with a bot token.
> Automated account use can get an account limited or terminated, and the web
> client's internal behaviour can change at any time. Archive only content you
> are authorised to access, and respect the privacy of the people whose
> messages you are collecting. You are responsible for how you use this.

## What it is not

- Not a bot: it registers nothing with Discord's developer platform.
- Not an API client: it makes no REST or gateway calls; it reads the rendered DOM.
- Not a way to reach other people's private servers: it sees exactly what the
  logged-in account can already see in the client.

## Install

Requires Python 3.10+ and **Google Chrome or Chromium installed on the system**.
dmine drives *your* browser and starts it as a long-running background
daemon, so a single login survives across runs (that is what the persistent
profile and the `--remote-debugging-port` daemon are for). Chrome/Chromium is
looked up in the usual install locations and on `PATH`.

If none is found it falls back to Playwright's bundled Chromium
(`playwright install chromium`), which works but does not give you the
long-lived daemon — the browser then starts and stops with each command.

Installed from source — there is no package on PyPI:

```bash
git clone https://github.com/hschaefer/dmine.git && cd dmine
python3 -m venv .venv && .venv/bin/pip install -e .
```

That gives you two entry points: the `dmine` CLI and `dmine-mcp`,
the MCP server (see [Use as an MCP server](#use-as-an-mcp-server)).

## One-time setup

```bash
dmine login
# prints a QR code (also written to --qr-out <path> if given)
# scan it with the Discord mobile app (Settings -> Scan QR code)
# the session token is injected into the browser profile automatically
```

Alternatively, log in manually:

```bash
dmine browser start
# a browser window opens -> log into Discord once (2FA fine)
```

The login persists in `~/.config/dmine/profile` (never re-asked).

## QR-code login (remote auth)

`dmine login` implements Discord's *remote auth* flow (the mechanism the
desktop app uses): it performs an RSA-OAEP key exchange with Discord's gateway,
renders `https://discord.com/ra/<fingerprint>` as a QR code, and — once you
scan it with the mobile app and confirm — exchanges the resulting ticket for a
regular session token, which is injected into the browser profile. No bot
token, no password, no API key.

> **This is an internal, undocumented flow.** At the time of writing the web
> client ships the QR-login component internally but exposes no reachable UI
> toggle for it, so this library speaks to the remote-auth gateway directly.
> Treat it as best-effort: it is not covered by Discord's public API contract
> and can break without notice.

The flow is also available as a library:

```python
from dmine.auth import RemoteAuth
from dmine.browser import DiscordBrowser

auth = RemoteAuth()
qr_url, png = auth.start()          # handshake + QR code (PNG bytes)
token, user = auth.wait_for_token() # blocks until scanned+confirmed

browser = DiscordBrowser().start()
browser.set_session(token, user)    # inject into the browser profile
browser.stop()
```

`set_session` writes the session via CDP directly into the browser profile's
storage backend (JSON-encoded exactly as Discord Web's storage wrapper
expects). It deliberately avoids Discord's obfuscated `localStorage` hiding —
no unstable internal property names are used, so injection survives client
updates as long as Discord keeps the JSON-encoding storage convention.

## Usage

```bash
dmine capture <channel-id-or-url> --full    # one-time backfill
dmine capture <channel-id-or-url>           # incremental since watermark
dmine search "keyword" [--channel <id>] [--server <id-or-name>]
dmine export <channel-id> --format md|jsonl [--out file]
dmine status [--channel <id>]               # watermark per channel
dmine channels                              # known channels
```

`search` is a case-insensitive substring match over message content, embeds and
author names — not a tokenised/ranked full-text index, and the searched columns
are unindexed, so a hit is found by scanning the archive. Fine for a personal
archive; expect it to get slower as the DB grows.

## Use as an MCP server

`dmine` is also an [MCP](https://modelcontextprotocol.io) server (stdio
transport), so any MCP-capable client — Claude Desktop, Cursor, OpenCode,
Hermes, … — can use the archive as structured tools instead of shelling out to
the CLI. The MCP SDK is a normal dependency; there is nothing extra to install.

Register it **by absolute path**: MCP clients do not inherit your shell's
`PATH` or virtualenv.

```json
{
  "mcpServers": {
    "dmine": {
      "command": "/absolute/path/to/dmine/.venv/bin/dmine-mcp"
    }
  }
}
```

`python -m dmine.mcp_server` is equivalent if you prefer to point the
client at an interpreter. `DMINE_*` variables (see below) can be passed
through the client's `env` block.

The tools fall into two groups:

| Tool | Needs the browser session | What it does |
|---|---|---|
| `recent` | no | most recent archived messages of a channel |
| `search` | no | substring search over content, embeds and author |
| `status` | no | total messages, per-channel watermark and counts |
| `servers` | no | known servers (id, name, channel and message counts) |
| `channels` | no | known channels, optionally of one server |
| `media` | no | archived attachments and embed images for a channel |
| `export` | no | export a channel to jsonl/markdown, sandboxed to `DMINE_EXPORT_DIR` |
| `capture` | **yes** | archive a channel (incremental or full backfill) |
| `remedia` | **yes** | re-download media for already archived messages |

The read tools work against the SQLite archive alone and are safe to call at
any time. `capture` and `remedia` drive a real browser, so they need the
one-time setup above first; they are serialized by a lock and can run for a
long time, so they return a `running` job summary instead of blocking past a
short client timeout.

## Architecture

- `dmine/` — Python package (CLI + capture + storage). Harness-agnostic;
  the CLI is the shared core.
- `dmine/mcp_server.py` — thin MCP adapter (`python -m dmine.mcp_server`,
  stdio) so any MCP-capable client can call the same core as structured tools.
- `archive.sqlite` — single source of truth (`messages`, `channels`, `servers`).
  Message IDs are snowflakes → `MAX(id)` per channel is the incremental watermark.
- `~/.config/dmine/media/<channel>/` — downloaded attachments
  (CDN links expire, so media is captured at capture time).

## Safety & limits

- DOM-only; no network interception, no tokens, no API calls.
- Scrolls with human-ish speed/jitter; stop conditions are convergence-based.
- Only archives channels the account can actually view.
- **Threat model:** the Chrome daemon exposes an unauthenticated CDP endpoint
  on 127.0.0.1 — any local process could attach and drive the logged-in
  Discord session. Keep the daemon profile on a trusted machine. See
  [SECURITY.md](SECURITY.md).
- **Concurrency:** all browser-driving operations take a non-blocking lock
  (`~/.config/dmine/capture.lock`, PID-verified, stale locks are
  reclaimed). DB reads (search/status/recent/…) are never locked.
- The archive holds other people's messages. Treat it as personal data: don't
  commit it, don't redistribute it, don't attach exports or channel
  screenshots to issues. `.gitignore` already excludes the archive, the
  browser profile, media and QR codes.

## Environment variables

| Var | Default |
|---|---|
| `DMINE_DB` | `~/.config/dmine/archive.sqlite` |
| `DMINE_PROFILE` | `~/.config/dmine/profile` |
| `DMINE_MEDIA` | `~/.config/dmine/media` |
| `DMINE_PORT` | `9223` |
| `DMINE_STATE` | `~/.config/dmine` (locks, pid files) |
| `DMINE_EXPORT_DIR` | `~/.config/dmine/exports` (MCP export sandbox) |
| `DMINE_MAX_MEDIA_MB` | `1024` (per-file download cap) |

## Tests

The suite is offline — no browser, no network, no Discord account, no existing
archive. It uses synthetic IDs only.

```bash
.venv/bin/python -m pytest -q
```

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE). Running a modified version as a
network service means section 13 requires you to offer its source to your users.

## Contributing note

The archive path can be overridden with `DMINE_DB`; the package is imported as `dmine`.
