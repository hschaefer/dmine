# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""Browser bootstrap: executable discovery and graceful degradation.

No browser is launched here. What is checked is the contract that made the tool
Linux-only before: with no system Chrome, start_daemon() must *return False* --
not raise -- so DiscordBrowser.start() can reach the bundled-Chromium fallback.
"""
import os

from dmine import browser


def test_resolve_executable_returns_an_existing_path_or_none():
    exe = browser.resolve_executable()
    assert exe is None or os.path.exists(exe)


def test_resolve_executable_degrades_to_none(monkeypatch):
    monkeypatch.setattr(browser.os.path, "exists", lambda p: False)
    monkeypatch.setattr(browser.shutil, "which", lambda name: None)
    assert browser.resolve_executable() is None


def test_resolve_executable_falls_back_to_path_lookup(monkeypatch):
    monkeypatch.setattr(browser.os.path, "exists", lambda p: False)
    monkeypatch.setattr(browser.shutil, "which", lambda name: "/opt/bin/" + name)
    assert browser.resolve_executable() == "/opt/bin/google-chrome"


def test_start_daemon_returns_false_without_executable(monkeypatch):
    monkeypatch.setattr(browser, "resolve_executable", lambda: None)
    monkeypatch.setattr(browser, "daemon_health", lambda port=browser.DEFAULT_PORT: False)
    # must not raise -- otherwise the bundled-Chromium fallback is unreachable
    assert browser.start_daemon(port=59999) is False


def test_start_daemon_short_circuits_when_already_healthy(monkeypatch):
    monkeypatch.setattr(browser, "daemon_health", lambda port=browser.DEFAULT_PORT: True)
    assert browser.start_daemon(port=59999) is True


def test_daemon_command_targets_the_resolved_binary(monkeypatch, tmp_path):
    seen = {}

    class _FakeSubprocess:
        DEVNULL = -3

        @staticmethod
        def Popen(cmd, **kwargs):          # never actually launch anything
            seen["cmd"] = cmd
            raise RuntimeError("stop before launch")

    monkeypatch.setattr(browser, "resolve_executable", lambda: "/opt/chrome")
    monkeypatch.setattr(browser, "daemon_health", lambda port=browser.DEFAULT_PORT: False)
    monkeypatch.setattr(browser, "subprocess", _FakeSubprocess)

    try:
        browser.start_daemon(port=59999, profile=tmp_path / "profile")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the fake Popen to raise")

    cmd = seen["cmd"]
    assert cmd[0] == "/opt/chrome"
    assert any(a.startswith("--remote-debugging-port=") for a in cmd)
    assert any(a.startswith("--user-data-dir=") for a in cmd)
    assert "--disable-blink-features=AutomationControlled" in cmd


class _FakeCtx:
    def __init__(self):
        self.pages = ["page-0"]


class _FakeChromium:
    def __init__(self, calls):
        self._calls = calls

    def connect_over_cdp(self, url):
        raise RuntimeError("no daemon listening")

    def launch_persistent_context(self, **kwargs):
        self._calls.update(kwargs)
        return _FakeCtx()


class _FakePlaywright:
    def __init__(self, calls):
        self.chromium = _FakeChromium(calls)

    def start(self):
        return self


def test_start_falls_back_to_bundled_chromium(monkeypatch, tmp_path):
    """The regression: this path used to be unreachable (start_daemon raised)."""
    calls = {}
    monkeypatch.setattr(browser, "resolve_executable", lambda: None)
    monkeypatch.setattr(browser, "daemon_health", lambda port=browser.DEFAULT_PORT: False)
    monkeypatch.setattr(browser, "sync_playwright", lambda: _FakePlaywright(calls))

    b = browser.DiscordBrowser(headless=False, port=59999, profile=tmp_path / "p")
    b.start()

    # no system binary -> Playwright's own Chromium, i.e. no executable_path
    assert "executable_path" not in calls
    assert calls["headless"] is False
    assert b.ctx is not None


def test_start_uses_the_system_binary_when_found(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(browser, "resolve_executable", lambda: "/opt/chrome")
    monkeypatch.setattr(browser, "daemon_health", lambda port=browser.DEFAULT_PORT: False)
    monkeypatch.setattr(browser, "sync_playwright", lambda: _FakePlaywright(calls))

    b = browser.DiscordBrowser(headless=True, port=59999, profile=tmp_path / "p")
    b.start()
    assert calls["executable_path"] == "/opt/chrome"
