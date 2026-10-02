# Changelog

Synced from the private development repository. Each entry is a snapshot;
individual commits are not mirrored.

## 2026-10-02 (75525845)

- release: 0.2.1
- docs: state the real browser requirement; stop calling substring search full-text
- browser: find Chrome/Chromium on any platform, and degrade instead of aborting

## 2026-10-02 (69f626ff)

- packaging: single-source the version
- release: 0.2.0
- cli: parse channel names from English client labels too
- release: attribute mirror commits to the GitHub account
- mcp: make the MCP surface first-class and discoverable
- release: no public commit when only excluded paths changed
- release: document the public commit identity and the pitfalls found while testing
- release: decide 'nothing to release' before writing the changelog
- release: env vars override -c user.* -- set the public identity explicitly
- release: plain apply before --3way, and advance the sync pointer on no-op releases
- release: pin the public commit identity instead of inheriting git config
- release: close two holes found while testing the pipeline
- release: fix empty-array expansion that passed a stray argument to the scanner
- release: private-to-public mirror tooling (transform rule, release + import scripts)
- ci: run the offline test suite on 3.10, 3.12 and 3.14
- docs: honest usage disclaimer, generic MCP client references, SECURITY.md
- license: AGPL-3.0-or-later (LICENSE, SPDX headers, packaging metadata)
- search: --server scoping (id or name) and server identity on every result
- scripts: move the live MCP smoke check out of pytest collection (it runs at import and has no test functions)
- gitignore: keep archive, browser profile, lock files and QR tickets out of the repo
- mcp: async job manager for long captures; English user-facing strings
- tests: replace personal/real Discord IDs with synthetic placeholders before publishing
- mcp: run sync Playwright core off the asyncio loop (capture/remedia via asyncio.to_thread)
- docs: explain why the remote-auth flow exists (no reachable QR tab in web client) and how session injection stays stable (CDP backend, no obfuscated property names)
- auth: QR-code login (remote auth protocol) as library + CLI
- review fixes: numeric snowflake comparisons everywhere, refresh-loop termination, PID-verified capture lock (O_EXCL, stale reclaim), remedia retries failed entries, atomic streaming media downloads + size cap + sanitized names, Archive close()/with (MCP fd leak), MCP export sandbox, set_json whitelist, LIKE escaping, --server wired, daemon PID file instead of fuser, login fast-fail headless, tab selection, export render dedupe; pytest suite (19 tests)
- archive-server: numeric channel ordering (snowflake IDs are strings; lexicographic sort put main-chat last)
- concurrency: backfill lock guard in do_capture (CLI+MCP), WAL+busy_timeout for concurrent reads
- archive-server: full server backfill with lock file; robust channel-name parsing
- embed images: extract + download + persist local paths; remedia cmd/tool; capture --refresh; server-id DB lookup + navigation guard; desc ordering for recent/media
- media: DMINE_MEDIA env override + MCP media tool (list/downloaded status per channel)
- mcp: dmine MCP server (capture/recent/search/status/channels/export) + shared do_capture; usable from any MCP-capable client
- browser: auto-headless daemon for cron contexts; storage: true MAX watermark; search covers embeds
- dmine: bot-free Discord archiver & miner (DOM-only, Playwright/CDP daemon, SQLite archive)

