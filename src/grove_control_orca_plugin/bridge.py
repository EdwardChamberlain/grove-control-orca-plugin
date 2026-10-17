"""Versioned handoff to Grove's hosted login and Print form."""

from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Protocol
from urllib.parse import urlencode

from .models import CancelCallback, GroveError, ProtocolError

PROTOCOL_VERSION = 1


class Window(Protocol):
    def post(self, data: dict) -> None: ...
    def close(self) -> None: ...
    def is_open(self) -> bool: ...


class WindowHost(Protocol):
    WINDOW_MODELESS: int
    WINDOW_MODAL: int

    def create_window(self, **kwargs) -> Window: ...


def navigation_html(url: str) -> str:
    """Navigate the whole webview, preserving Grove's normal origin and frame policy."""
    value = json.dumps(url).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (
        '<!doctype html><meta charset="utf-8"><title>Grove Control</title>'
        "<p>Opening Grove Control…</p><script>window.location.replace(" + value + ");</script>"
    )


def print_frame_html(url: str) -> str:
    """Bind secret-bearing initialization to the configured Grove origin.

    Orca's host-window message callback has no authenticated sender origin. The
    local wrapper uses browser-enforced origin and source checks instead; a
    redirected frame cannot receive a session token or impersonate Grove.
    """
    value = json.dumps(url).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (
        '<!doctype html><meta charset="utf-8"><title>Add to Grove queue</title>'
        "<style>html,body{margin:0;height:100%;overflow:hidden}"
        "iframe{width:100%;height:100%;border:0}</style>"
        '<iframe id="grove" title="Grove Print form" referrerpolicy="no-referrer"></iframe>'
        '<script>const frame=document.getElementById("grove");'
        f"const target=new URL({value});"
        'window.addEventListener("message",event=>{'
        "if(event.origin===target.origin&&event.source===frame.contentWindow)"
        "window.orca.postMessage(event.data);});"
        "window.orca.onMessage(data=>frame.contentWindow.postMessage(data,target.origin));"
        "frame.src=target.href;</script>"
    )


class RemoteExchange:
    """One window and one request nonce. Never interpret a closed window as success."""

    def __init__(
        self,
        host: WindowHost,
        grove_url: str,
        mode: str,
        initialization: dict,
        cancelled: CancelCallback = lambda: False,
        handshake_timeout: float = 20,
    ):
        self.host = host
        self.mode = mode
        self.request_id = secrets.token_urlsafe(24)
        query = urlencode({"mode": mode, "protocol_version": PROTOCOL_VERSION, "request_id": self.request_id})
        self.url = grove_url + "/integrations/orca?" + query
        self._initialization = initialization
        self._cancelled = cancelled
        self._handshake_timeout = handshake_timeout
        self._lock = threading.RLock()
        self._done = threading.Event()
        self._ready = threading.Event()
        self._window: Window | None = None
        self._outcome: dict = {}
        self._error: GroveError | None = None
        self.opened = False

    def _envelope(self, kind: str, **data) -> dict:
        return {**data, "type": kind, "protocol_version": PROTOCOL_VERSION, "request_id": self.request_id}

    def _initialize(self) -> None:
        with self._lock:
            window = self._window
            if window is None or not self._ready.is_set() or self._done.is_set():
                return
            payload = self._envelope("initialize", mode=self.mode, **self._initialization)
        # UI calls can marshal to the main thread and re-enter receive(). Never hold our lock.
        # Grove asks again after a page reload. Initialization must never auto-submit.
        window.post(payload)

    def receive(self, data: object) -> None:
        if not isinstance(data, dict) or data.get("request_id") != self.request_id:
            return
        with self._lock:
            if self._done.is_set():
                return
            if type(data.get("protocol_version")) is not int or data["protocol_version"] != PROTOCOL_VERSION:
                self._error = ProtocolError("This Grove page uses an unsupported Orca bridge version.")
                self._done.set()
                return
            kind = data.get("type")
            if kind == "ready":
                self._ready.set()
            elif kind in ("authenticated", "submitted", "partial", "cancelled", "session_expired", "error"):
                expected = (
                    {"authenticated", "cancelled", "error"}
                    if self.mode == "connect"
                    else {"submitted", "partial", "cancelled", "session_expired", "error"}
                )
                if kind not in expected or not self._ready.is_set():
                    self._error = ProtocolError("Grove sent an unexpected integration event.")
                else:
                    self._outcome = data
                self._done.set()
                return
            else:
                return
        self._initialize()

    def closed(self) -> None:
        with self._lock:
            if not self._done.is_set():
                self._outcome = {"type": "closed"}
                self._done.set()

    def interrupt(self) -> None:
        with self._lock:
            if not self._done.is_set():
                self._outcome = {"type": "interrupted"}
                self._done.set()

    def run(self) -> dict:
        if self._cancelled():
            return {"type": "interrupted"}
        self._window = self.host.create_window(
            html=navigation_html(self.url) if self.mode == "connect" else print_frame_html(self.url),
            title="Sign in to Grove Control" if self.mode == "connect" else "Add to Grove queue",
            width=1040,
            height=820,
            # Print runs inside Orca's modal Send flow. A modeless sibling is
            # disabled behind it; use the host's nested modal window instead.
            # create_window still returns a handle immediately, so messaging,
            # cancellation, and receipt handling remain on this worker.
            style=self.host.WINDOW_MODAL if self.mode == "print" else self.host.WINDOW_MODELESS,
            on_message=self.receive,
            on_close=self.closed,
        )
        self.opened = True
        self._initialize()
        started = time.monotonic()
        cancel_requested_at: float | None = None
        try:
            while not self._done.wait(0.1):
                now = time.monotonic()
                if not self._window.is_open():
                    self.closed()
                    continue
                if not self._ready.is_set() and now - started >= self._handshake_timeout:
                    raise ProtocolError(
                        "Grove did not start the Orca integration page. "
                        "It requires the Grove-side integration."
                    )
                if self._cancelled():
                    if cancel_requested_at is None:
                        cancel_requested_at = now
                        self._window.post(self._envelope("cancel_requested"))
                    elif now - cancel_requested_at >= 3:
                        self.interrupt()
            if self._error:
                raise self._error
            return self._outcome
        finally:
            self._initialization = {}  # Release the window's copy of a session token.
            self._outcome = {}
            if self._window.is_open():
                self._window.close()
