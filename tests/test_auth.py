# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""Tests for dmine.auth — remote auth (QR login) protocol.

Spins up a local mock gateway WebSocket + token exchange HTTP server and
verifies the full handshake, QR generation, scan flow and token decryption.
"""
import asyncio
import base64
import json
import threading

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from dmine.auth import RemoteAuth, RemoteAuthError, parse_user_payload, render_qr_png

MOCK_USER = {"id": "100000000000000002", "discriminator": "0",
             "avatar": "00000000000000000000000000000000", "username": "testuser"}
MOCK_TOKEN = "mock-token-for-test-only"


def _oaep():
    return padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                        algorithm=hashes.SHA256(), label=None)


class MockGateway:
    """Simulates Discord's remote-auth gateway on a local WebSocket."""

    def __init__(self, wrong_fingerprint: bool = False):
        self.port = None
        self._server = None
        self._thread = None
        self._public_key = None
        self.wrong_fingerprint = wrong_fingerprint

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # wait until bound
        import time
        deadline = time.time() + 5
        while self.port is None and time.time() < deadline:
            time.sleep(0.02)
        assert self.port, "mock gateway failed to bind"

    def _run(self):
        import websockets

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def handler(ws):
            # hello
            await ws.send(json.dumps({"op": "hello", "timeout_ms": 120000,
                                      "heartbeat_interval": 30000}))
            # init -> nonce_proof
            msg = json.loads(await ws.recv())
            assert msg["op"] == "init"
            self._public_key = serialization.load_der_public_key(
                base64.b64decode(msg["encoded_public_key"]))
            nonce = b"mock-nonce-0123456789abcdef"
            encrypted_nonce = self._public_key.encrypt(nonce, _oaep())
            await ws.send(json.dumps({"op": "nonce_proof",
                                      "encrypted_nonce": base64.b64encode(encrypted_nonce).decode()}))
            # nonce_proof reply -> pending_remote_init (fingerprint)
            msg = json.loads(await ws.recv())
            assert msg["op"] == "nonce_proof"
            spki = self._public_key.public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo)
            fingerprint = base64.urlsafe_b64encode(
                hashes.Hash(hashes.SHA256()).update(spki).finalize() if False else
                __import__("hashlib").sha256(spki).digest()
            ).decode().rstrip("=")
            if self.wrong_fingerprint:
                fingerprint = "WRONG-FINGERPRINT"
            await ws.send(json.dumps({"op": "pending_remote_init", "fingerprint": fingerprint}))

            # consume heartbeats; simulate the scan immediately after handshake
            user_payload = ":".join([MOCK_USER["id"], MOCK_USER["discriminator"],
                                     MOCK_USER["avatar"], MOCK_USER["username"]]).encode()
            encrypted_payload = self._public_key.encrypt(user_payload, _oaep())
            await ws.send(json.dumps({"op": "pending_ticket",
                                      "encrypted_user_payload": base64.b64encode(encrypted_payload).decode()}))
            await ws.send(json.dumps({"op": "pending_login", "ticket": "mock-ticket-123"}))
            try:
                while True:
                    msg = json.loads(await ws.recv())
                    if msg["op"] == "heartbeat":
                        await ws.send(json.dumps({"op": "heartbeat_ack"}))
            except Exception:
                pass  # client closed the connection after login — expected

        import socket as _socket

        # bind a socket first so we know the port regardless of websockets API drift
        probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.listen(1)
        probe.setblocking(False)

        async def _bind():
            # websockets>=13: serve() is async and needs a running loop; pass the socket in
            return await websockets.serve(handler, sock=probe)

        self._server = self._loop.run_until_complete(_bind())
        self._loop.run_forever()

    def stop(self):
        if self._server is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._server.close()


class MockExchange:
    """Local HTTP server that answers the ticket -> encrypted_token exchange."""

    def __init__(self, gateway):
        self.port = None
        self._server = None
        self._thread = None
        self._gateway = gateway

    def start(self):
        import http.server
        import socketserver

        gateway_ref = self._gateway

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                assert "ticket" in body
                # reuse gateway's public key for the encrypted token
                encrypted = gateway_ref._public_key.encrypt(MOCK_TOKEN.encode(), _oaep())
                payload = json.dumps({"encrypted_token": base64.b64encode(encrypted).decode()}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()


@pytest.fixture()
def gateway():
    g = MockGateway()
    g.start()
    yield g
    g.stop()


@pytest.fixture()
def exchange(gateway):
    e = MockExchange(gateway)
    e.start()
    yield e
    e.stop()


def test_parse_user_payload():
    raw = "100000000000000002:0:00000000000000000000000000000000:testuser"
    assert parse_user_payload(raw) == MOCK_USER
    with pytest.raises(RemoteAuthError):
        parse_user_payload("not-a-payload")


def test_render_qr_png():
    png = render_qr_png("https://discord.com/ra/abc123")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 100


def test_full_remote_auth_flow(gateway, exchange):
    auth = RemoteAuth(timeout_s=30, gateway_url=f"ws://127.0.0.1:{gateway.port}",
                      origin="https://discord.com", exchange_url=f"http://127.0.0.1:{exchange.port}")
    scanned = []
    qr_url, png = auth.start(on_user_scanned=lambda u: scanned.append(u))
    assert qr_url.startswith("https://discord.com/ra/")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert auth.fingerprint is not None

    token, user = auth.wait_for_token(timeout_s=15)
    assert token == MOCK_TOKEN
    assert user == MOCK_USER
    assert scanned == [MOCK_USER]


def test_handshake_rejects_bad_fingerprint():
    """A gateway that returns a wrong fingerprint must fail fast."""
    g = MockGateway(wrong_fingerprint=True)
    g.start()
    try:
        auth = RemoteAuth(timeout_s=15, gateway_url=f"ws://127.0.0.1:{g.port}",
                          origin="https://discord.com")
        with pytest.raises(RemoteAuthError, match="fingerprint mismatch"):
            auth.start()
    finally:
        g.stop()
