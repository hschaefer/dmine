# Security

## Reporting

Open a private security advisory on the repository (Security → Report a
vulnerability) rather than a public issue. Please include the version/commit,
the platform, and a minimal reproduction. There is no bounty program.

## Threat model

dmine drives a real browser session that is logged into a Discord
account, so most of its attack surface is *local*, not network-facing.

### 1. Unauthenticated CDP endpoint (by design, local only)

While the daemon is running, Chrome exposes a Chrome DevTools Protocol endpoint
on `127.0.0.1` (`DMINE_PORT`, default `9223`) with **no authentication**.

Consequences, all local:

- Any process running as any user on the same machine that can reach the port
  can attach to the browser and act with the full privileges of the logged-in
  Discord session, including reading the session token from the profile.
- Anything that can inject into the page (a malicious extension, a compromised
  local dependency) inherits the same capability.

Mitigations:

- Keep the daemon profile and archive on a machine you trust, with no untrusted
  local users or processes.
- Bind/verify that the port is loopback-only (`ss -ltnp | grep <port>`); do not
  expose it through container networking, a proxy, or SSH port-forwarding.
- Stop the daemon (`dmine browser stop`) when you are not capturing.

This is an accepted trade-off for a single-user local tool. A remote or
multi-user deployment is **not** supported by the current design.

### 2. Session token at rest

The browser profile under `~/.config/dmine/profile` contains a live
Discord session token. It is protected by filesystem permissions only, not by
application-level encryption.

- Never commit the profile (`.gitignore` excludes it) and never copy it between
  machines.
- A stolen token is equivalent to a stolen login: `dmine login` again to
  rotate the session if you suspect exposure.

### 3. Captured data is other people's personal data

The SQLite archive and downloaded media contain messages, user IDs, avatars and
attachments belonging to third parties. They are personal data.

- Do not attach archive files, exports or screenshots of real channels to
  issues, pull requests or bug reports. Use a scratch archive or synthetic IDs.
- `DMINE_EXPORT_DIR` exists so MCP-driven exports land in a directory
  you can wipe; the MCP `export` tool is sandboxed to it.

### 4. Untrusted input in the capture path

Channel names, attachment filenames and message content come from Discord and
are not trusted.

- Attachment filenames are sanitized and downloads are capped
  (`DMINE_MAX_MEDIA_MB`) with atomic writes.
- The SQLite layer escapes `LIKE` patterns and uses a whitelist for JSON
  field updates.
- Reports of a bypass (path traversal via a crafted filename, SQL/`LIKE`
  injection, write outside the export dir) are in scope and welcome.

## Out of scope

- Discord ToS enforcement actions against a user's account (see the README:
  automating a user account is not a sanctioned use).
- Anything requiring an attacker who already has code execution as your user —
  at that point the local CDP endpoint is the least of your problems.
