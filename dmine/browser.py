# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""Browser management for dmine.

Two modes:
  * daemon (default): connects over CDP to a long-running Chrome instance
    started via `dmine browser start` (--remote-debugging-port).
    The browser stays alive between captures, so the Discord login persists
    like a normal browser session — required for unattended cron runs.
  * standalone fallback: launches its own persistent-context instance
    (headless/headful) when no daemon is reachable.

DOM-only: no API tokens, no network interception — the archive sees exactly
what the account can see.

Threat model note: the CDP endpoint binds to 127.0.0.1 only, but any local
process can attach and drive the logged-in Discord session. Keep the daemon
profile on a trusted machine.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import httpx
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

DEFAULT_PROFILE = Path(os.environ.get("DMINE_PROFILE", Path.home() / ".config" / "dmine" / "profile"))
DEFAULT_PORT = int(os.environ.get("DMINE_PORT", "9223"))
DISCORD_APP = "https://discord.com/app"


def resolve_executable() -> str | None:
    for cand in (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ):
        if os.path.exists(cand):
            return cand
    return None


def daemon_health(port: int = DEFAULT_PORT) -> bool:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _pid_path(port: int = DEFAULT_PORT) -> Path:
    state = Path(os.environ.get("DMINE_STATE", Path.home() / ".config" / "dmine"))
    return state / f"daemon-{port}.pid"


def start_daemon(port: int = DEFAULT_PORT, profile: str | Path = DEFAULT_PROFILE, url: str = DISCORD_APP) -> bool:
    """Launch the persistent Chrome daemon detached (keeps running).

    Headful when a DISPLAY exists (user desktop), headless=new otherwise
    (cron/system context) — the profile carries the login either way.
    Records the PID so stop_daemon can kill precisely (no port guessing).
    """
    if daemon_health(port):
        return True
    exe = resolve_executable()
    if not exe:
        raise RuntimeError("no chrome/chromium executable found")
    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
    ]
    if not os.environ.get("DISPLAY"):
        cmd.append("--headless=new")
    cmd.append(url)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _pid_path(port).write_text(str(proc.pid))
    for _ in range(30):
        if daemon_health(port):
            return True
        time.sleep(0.5)
    return False


def stop_daemon(port: int = DEFAULT_PORT) -> bool:
    """Stop the daemon by its recorded PID (verified against the cmdline)."""
    pid_file = _pid_path(port)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            cmdline = Path(f"/proc/{pid}/cmdline")
            if cmdline.exists() and b"remote-debugging-port" in cmdline.read_bytes():
                os.kill(pid, 15)  # SIGTERM first; Chrome exits cleanly
                for _ in range(20):
                    if not cmdline.exists():
                        break
                    time.sleep(0.3)
                else:
                    os.kill(pid, 9)
                pid_file.unlink(missing_ok=True)
                return True
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass
        pid_file.unlink(missing_ok=True)  # stale pid file
    return False


class DiscordBrowser:
    def __init__(self, headless: bool = False, port: int | None = None, profile: str | Path | None = None):
        self.headless = headless
        self.port = port or DEFAULT_PORT
        self.profile = Path(profile) if profile else DEFAULT_PROFILE
        self._pw = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None
        self.attached = False

    def _attach(self, url: str) -> tuple[Browser, BrowserContext]:
        b = self._pw.chromium.connect_over_cdp(url)
        ctx = b.contexts[0] if b.contexts else b.new_context()
        return b, ctx

    def start(self) -> "DiscordBrowser":
        self._pw = sync_playwright().start()
        try:
            # 1) attach to a running daemon
            if daemon_health(self.port):
                try:
                    _, self.ctx = self._attach(f"http://127.0.0.1:{self.port}")
                    self.attached = True
                    self.page = self._pick_page()
                    return self
                except Exception:
                    pass
            # 2) start the daemon (headful on the desktop) and attach
            if not self.headless and start_daemon(self.port, self.profile):
                _, self.ctx = self._attach(f"http://127.0.0.1:{self.port}")
                self.attached = True
                self.page = self._pick_page()
                return self
            # 3) standalone persistent context (short-lived; login may not survive)
            exe = resolve_executable()
            kwargs = dict(
                user_data_dir=str(self.profile),
                headless=self.headless,
                viewport={"width": 1680, "height": 950},
                locale="en-US",
            )
            if exe:
                kwargs["executable_path"] = exe
            self.ctx = self._pw.chromium.launch_persistent_context(**kwargs)
            self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
            return self
        except Exception:
            self.stop()
            raise

    def _pick_page(self) -> Page:
        """Prefer an existing Discord tab; never hijack an arbitrary tab 0."""
        if self.ctx:
            for p in self.ctx.pages:
                if (p.url or "").startswith("https://discord.com"):
                    return p
        return self.ctx.new_page() if self.ctx else self.page

    def stop(self) -> None:
        try:
            if self.attached:
                self._pw.stop()  # detach only — the daemon keeps running
            else:
                if self.ctx:
                    self.ctx.close()
                self._pw.stop()
        finally:
            self._pw = None

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)

    def is_logged_in(self) -> bool:
        if not self.page:
            return False
        if "/channels/" in (self.page.url or ""):
            return True
        try:
            return self.page.locator(
                '[data-list-id="chat-messages"], nav[class*="guilds"], [data-list-id="guildsnav"]'
            ).first.is_visible(timeout=1500)
        except Exception:
            return False

    def wait_for_login(self, timeout_s: int = 1800) -> bool:
        """Block until the Discord app shell appears (user completes login manually)."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.is_logged_in():
                return True
            time.sleep(2)
        return False

    def ensure_logged_in(self, timeout_s: int = 1800) -> None:
        self.goto(DISCORD_APP)
        # the SPA redirects /app -> /channels/<...> client-side; give it a moment
        self.page.wait_for_timeout(3000)
        if self.is_logged_in():
            return
        print("LOGIN_REQUIRED: complete the login in the browser window that just opened.", flush=True)
        if not self.wait_for_login(timeout_s):
            raise TimeoutError(
                "Discord login not completed in time. "
                "Run `dmine login` for QR-code login, or log in manually "
                "(dmine browser start → im Fenster anmelden)."
            )
        print("LOGIN_OK", flush=True)

    # ------------------------------------------------------- session injection --

    # How Discord Web stores the session (reverse-engineered 2026-08):
    #   * The web client hides window.localStorage at boot (it copies the
    #     reference to a private, obfuscated property and deletes the
    #     accessor) and reads tokens through a wrapper that JSON-decodes
    #     stored values. Writing the raw token string is therefore NOT
    #     enough: the token must be stored JSON-encoded, alongside the
    #     per-user "tokens" map and a MultiAccountStore entry with
    #     tokenStatus 2.
    #   * We deliberately write via CDP DOMStorage (the browser's storage
    #     backend) instead of page JS — that bypasses the localStorage
    #     hiding entirely and never touches the obfuscated property name,
    #     so a rename of that property in a future client build does not
    #     break injection. The only client-internal detail we depend on is
    #     the JSON-encoding convention.
    _STORAGE_ORIGIN = "https://discord.com"

    def set_session(self, token: str, user: dict) -> bool:
        """Inject an authentication token into the browser's Discord session.

        ``user`` must contain ``id``, ``username``, ``avatar`` and
        ``discriminator`` (as returned by :meth:`dmine.auth.RemoteAuth.wait_for_token`).

        The token is written into the Chrome profile's localStorage via CDP
        (``DOMStorage``), JSON-encoded exactly as Discord Web's storage
        wrapper expects. Afterwards the app is reloaded in a fresh tab so the
        token manager re-reads the session at boot.

        Returns ``True`` if the app ends up logged in.
        """
        import json as _json

        if self.ctx is None or self.page is None:
            raise RuntimeError("browser must be started before set_session()")
        uid = str(user["id"])
        store = {
            "_state": {
                "users": [
                    {
                        "id": uid,
                        "avatar": user.get("avatar") or "0",
                        "username": user.get("username") or uid,
                        "discriminator": str(user.get("discriminator") or "0"),
                        "tokenStatus": 2,
                        "pushSyncToken": None,
                    }
                ],
                "canUseMultiAccountMobile": False,
            },
            "_version": 1,
        }

        # Write via CDP DOMStorage — bypasses Discord's localStorage hiding.
        cdp = self.ctx.new_cdp_session(self.page)
        sid = {"securityOrigin": self._STORAGE_ORIGIN, "isLocalStorage": True}
        for key, value in (
            ("token", _json.dumps(token)),
            ("tokens", _json.dumps({uid: token})),
            ("MultiAccountStore", _json.dumps(store)),
        ):
            cdp.send("DOMStorage.setDOMStorageItem", {"storageId": sid, "key": key, "value": value})

        # Fresh tab → token manager reads the session at boot.
        page2 = self.ctx.new_page()
        page2.goto(DISCORD_APP, wait_until="domcontentloaded", timeout=60000)
        page2.wait_for_timeout(12000)
        ok = self._is_logged_in_page(page2)
        self.page = page2  # make the freshly-logged-in tab the working page
        return ok

    @staticmethod
    def _is_logged_in_page(page: Page) -> bool:
        if "/channels/" in (page.url or ""):
            return True
        try:
            return page.locator(
                '[data-list-id="chat-messages"], nav[class*="guilds"], [data-list-id="guildsnav"]'
            ).first.is_visible(timeout=9000)
        except Exception:
            return False

    def login_with_qr(self, timeout_s: int = 300, qr_out: str | None = None) -> bool:
        """Run the full QR-code login flow and inject the session.

        Requires the optional auth dependencies (``websockets``,
        ``cryptography``, ``qrcode``). If ``qr_out`` is given, the QR code
        PNG is written to that path so the user can scan it (e.g. over chat).
        """
        from .auth import RemoteAuth, RemoteAuthError

        try:
            auth = RemoteAuth(timeout_s=timeout_s)
            qr_url, png = auth.start()
        except RemoteAuthError as exc:
            print(f"LOGIN_ERROR: {exc}", flush=True)
            return False
        except ImportError as exc:  # pragma: no cover - dependency check
            print(f"LOGIN_ERROR: missing dependency ({exc}); pip install websockets cryptography qrcode", flush=True)
            return False

        print(f"SCAN_THIS_URL: {qr_url}", flush=True)
        if qr_out:
            Path(qr_out).write_bytes(png)
            print(f"QR_CODE_WRITTEN: {qr_out}", flush=True)
        else:
            print("QR_CODE_READY (scan with the Discord mobile app)", flush=True)

        try:
            token, user = auth.wait_for_token(timeout_s=timeout_s)
        except RemoteAuthError as exc:
            print(f"LOGIN_ERROR: {exc}", flush=True)
            return False

        print(f"LOGIN_SCANNED: {user.get('username')} ({user.get('id')})", flush=True)
        ok = self.set_session(token, user)
        print("LOGIN_OK" if ok else "LOGIN_INJECT_FAILED", flush=True)
        return ok
