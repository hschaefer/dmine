# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""Discord remote authentication (QR-code login) — bot-free, API-free.

Implements the *remote auth* protocol that the official Discord desktop
client uses to log a device in via the mobile app: the desktop side opens
a WebSocket to Discord's remote-auth gateway, performs an RSA-OAEP key
exchange, and renders a QR code containing ``https://discord.com/ra/<fingerprint>``.
Once the user scans the code in the mobile app and confirms, the gateway
pushes a ticket which can be exchanged for a regular authentication token.

No bot token, no password, no API key — the token obtained is the *same*
token a normal browser session uses, and it is only ever injected into a
browser profile the user controls.

Why not use Discord Web's built-in QR login?
--------------------------------------------
Discord's web client *does* ship the QR-login component (the ``qrLogin``
panel is present in the login DOM, hidden via ``display:none``), but the
current web client exposes **no reachable UI toggle** for it — no tab
switch on the login form, account picker, or "add account" screen (checked
on 2026-08; the QR tab only exists in the desktop app). Forcing the React
state from outside would be more fragile than implementing the protocol
directly, so this module speaks to the remote-auth gateway itself. The
resulting token is identical to what the browser flow would produce.

Example
-------
.. code-block:: python

    from dmine.auth import RemoteAuth

    auth = RemoteAuth()
    qr_url, png_bytes = auth.start()          # handshake + QR code
    print(f"Scan this in the Discord app: {qr_url}")

    token, user = auth.wait_for_token()       # blocks until scanned+confirmed
    # user == {"id": ..., "username": ..., "avatar": ..., "discriminator": ...}

    # hand the token to a DiscordBrowser (see browser.DiscordBrowser.set_session)
"""
from __future__ import annotations

import base64
import concurrent.futures as _futures
import hashlib
import json
import threading
import time
import urllib.request
from typing import Any, Callable, cast

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

__all__ = [
    "RemoteAuth",
    "RemoteAuthError",
    "parse_user_payload",
    "render_qr_png",
    "GATEWAY_URL",
    "EXCHANGE_URL",
]

GATEWAY_URL = "wss://remote-auth-gateway.discord.gg/?v=2"
EXCHANGE_URL = "https://discord.com/api/v9/users/@me/remote-auth/login"
GATEWAY_ORIGIN = "https://discord.com"


class RemoteAuthError(RuntimeError):
    """Raised when the remote-auth handshake or login exchange fails."""


def parse_user_payload(raw: str) -> dict:
    """Parse the colon-separated user payload from a ``pending_ticket`` event.

    Format: ``<id>:<discriminator>:<avatar>:<username>``
    """
    try:
        uid, discriminator, avatar, username = raw.split(":", 3)
    except ValueError as exc:  # pragma: no cover - defensive
        raise RemoteAuthError(f"malformed user payload: {raw!r}") from exc
    if not uid.isdigit():
        raise RemoteAuthError(f"malformed user payload (bad id): {raw!r}")
    return {
        "id": uid,
        "discriminator": discriminator,
        "avatar": avatar,
        "username": username,
    }


def render_qr_png(url: str, *, box_size: int = 10, border: int = 4) -> bytes:
    """Render a QR code for *url* and return it as PNG bytes."""
    import io

    import qrcode  # deferred: only needed when actually logging in

    img = qrcode.make(url, box_size=box_size, border=border)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _oaep() -> padding.OAEP:
    return padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()),
        algorithm=hashes.SHA256(),
        label=None,
    )


class RemoteAuth:
    """A single Discord remote-auth (QR login) session.

    Usage
    -----
    1. ``auth = RemoteAuth()``
    2. ``qr_url, png = auth.start()`` — performs the WebSocket handshake and
       returns the QR content plus a PNG rendering of it.
    3. ``token, user = auth.wait_for_token()`` — blocks (up to ``timeout_s``)
       until the user scans the code in the mobile app and confirms.
    4. ``auth.close()`` — releases the worker thread (idempotent).

    The session is single-use: after ``wait_for_token()`` the gateway closes
    the connection and the ticket has been consumed.
    """

    def __init__(
        self,
        timeout_s: int = 300,
        gateway_url: str = GATEWAY_URL,
        origin: str = GATEWAY_ORIGIN,
        exchange_url: str = EXCHANGE_URL,
    ):
        self.timeout_s = timeout_s
        self.gateway_url = gateway_url
        self.origin = origin
        self.exchange_url = exchange_url
        self.fingerprint: str | None = None
        self.qr_url: str | None = None
        self.started_at: float | None = None
        self._future: _futures.Future | None = None
        self._thread: threading.Thread | None = None
        self._on_user_scanned: Callable[[dict], None] | None = None

    # ------------------------------------------------------------- public --

    def start(self, on_user_scanned: Callable[[dict], None] | None = None) -> tuple[str, bytes]:
        """Run the handshake and return ``(qr_url, qr_png_bytes)``.

        ``on_user_scanned(user_dict)`` is invoked (best-effort) once the
        mobile app has scanned the code, before the user confirms.
        """
        self._on_user_scanned = on_user_scanned
        self.started_at = time.time()
        self._future = _futures.Future()
        self._thread = threading.Thread(
            target=self._worker, name="dmine-remote-auth", daemon=True
        )
        self._thread.start()
        # wait until the handshake finished and qr_url is set
        deadline = time.time() + 30
        while self.qr_url is None:
            if self._future.done():
                raise RemoteAuthError(f"remote auth failed during handshake: {self._future.exception()}")
            if time.time() > deadline:
                raise RemoteAuthError("timed out during remote-auth handshake")
            time.sleep(0.05)
        assert self.qr_url is not None
        return self.qr_url, render_qr_png(self.qr_url)

    def wait_for_token(self, timeout_s: int | None = None) -> tuple[str, dict]:
        """Block until the user scans+confirms; return ``(token, user_dict)``.

        Raises :class:`RemoteAuthError` on cancel/timeout/network failure.
        """
        if self._future is None:
            raise RemoteAuthError("start() must be called before wait_for_token()")
        timeout_s = timeout_s or self.timeout_s
        try:
            result = self._future.result(timeout=timeout_s)
        except _futures.TimeoutError as exc:
            raise RemoteAuthError(
                f"timed out after {timeout_s}s waiting for the QR code to be scanned"
            ) from exc
        except Exception as exc:
            raise RemoteAuthError(str(exc)) from exc
        finally:
            self.close()
        return result

    def close(self) -> None:
        """Stop the worker (idempotent). Safe to call from any thread."""
        if self._future is not None and not self._future.done():
            self._future.cancel()
        # the worker thread is a daemon; nothing else to join eagerly

    def __enter__(self) -> "RemoteAuth":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------- internals --

    def _worker(self) -> None:
        try:
            import asyncio

            asyncio.run(self._run())
        except Exception as exc:  # pragma: no cover - defensive
            if self._future is not None and not self._future.done():
                self._future.set_exception(RemoteAuthError(str(exc)))

    async def _run(self) -> None:
        import asyncio

        import websockets

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key()
        spki = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        encoded_pub = base64.b64encode(spki).decode()

        async with websockets.connect(
            self.gateway_url, origin=cast(Any, self.origin), max_size=2**20
        ) as ws:
            # -- hello -> init -> nonce_proof -> pending_remote_init --
            hello = json.loads(await ws.recv())
            if hello.get("op") != "hello":
                raise RemoteAuthError(f"unexpected gateway hello: {hello}")
            await ws.send(json.dumps({"op": "init", "encoded_public_key": encoded_pub}))

            msg = json.loads(await ws.recv())
            if msg.get("op") != "nonce_proof":
                raise RemoteAuthError(f"unexpected init reply: {msg}")
            nonce = private_key.decrypt(base64.b64decode(msg["encrypted_nonce"]), _oaep())
            nonce_proof = base64.urlsafe_b64encode(nonce).decode().rstrip("=")
            await ws.send(json.dumps({"op": "nonce_proof", "nonce": nonce_proof}))

            msg = json.loads(await ws.recv())
            if msg.get("op") != "pending_remote_init":
                raise RemoteAuthError(f"unexpected nonce reply: {msg}")
            fingerprint = msg["fingerprint"]

            digest = base64.urlsafe_b64encode(hashlib.sha256(spki).digest()).decode().rstrip("=")
            if digest != fingerprint:
                raise RemoteAuthError("fingerprint mismatch (gateway did not echo our key)")

            self.fingerprint = fingerprint
            self.qr_url = f"https://discord.com/ra/{fingerprint}"

            # -- heartbeat task --
            hb_interval = hello.get("heartbeat_interval", 41250) / 1000.0

            async def heartbeat():
                try:
                    while True:
                        await asyncio.sleep(max(hb_interval * 0.9, 1.0))
                        await ws.send(json.dumps({"op": "heartbeat"}))
                except Exception:
                    pass

            hb_task = asyncio.create_task(heartbeat())

            # -- wait for scan + confirmation --
            ticket: str | None = None
            scanned_user: dict = {}
            try:
                while ticket is None:
                    msg = json.loads(await ws.recv())
                    op = msg.get("op")
                    if op == "pending_ticket":
                        raw = private_key.decrypt(
                            base64.b64decode(msg["encrypted_user_payload"]), _oaep()
                        )
                        scanned_user = parse_user_payload(raw.decode())
                        if self._on_user_scanned:
                            try:
                                self._on_user_scanned(scanned_user)
                            except Exception:
                                pass
                    elif op == "pending_login":
                        ticket = msg.get("ticket")
                    elif op == "cancel":
                        raise RemoteAuthError("remote auth canceled on the mobile side")
            finally:
                hb_task.cancel()

            # -- exchange ticket for token --
            req = urllib.request.Request(
                self.exchange_url,
                data=json.dumps({"ticket": ticket}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (dmine)",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read().decode())
            except Exception as exc:
                raise RemoteAuthError(f"ticket exchange failed: {exc}") from exc

            encrypted_token = body.get("encrypted_token")
            if not encrypted_token:
                raise RemoteAuthError(f"ticket exchange returned no token: {body}")
            token = private_key.decrypt(
                base64.b64decode(encrypted_token), _oaep()
            ).decode()

            if self._future is not None and not self._future.done():
                self._future.set_result((token, scanned_user))
