"""HTTP client for the Reolink CGI/JSON API (``/cgi-bin/api.cgi``).

Standard library only. Commands are POSTed as a JSON list and answered with a list of
``{"cmd", "code", "value" | "error"}`` objects; a token from ``Login`` authenticates
everything else. The client is thread-safe: JSON commands are serialised per camera
(Reolink firmware does not like concurrent requests), while file downloads run
alongside them.

Cameras use self-signed certificates on their IP address, so TLS verification is
disabled for camera connections, as the official apps and Home Assistant do.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .errors import ApiError, AuthError, Cancelled, ConnectionFailed, ReolinkError

MAX_BATCH = 30          # firmware answers -16 "send failed" above ~35 commands per request
TOKEN_MARGIN = 300      # renew the token when less than this many seconds remain

_CRED_DETAILS = ("invalid user", "login failed", "password wrong", "login has been locked")


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        # Older camera firmware only offers ciphers that OpenSSL 3 treats as legacy.
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError:
        pass
    return ctx


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib API
        return None


def _opener() -> urllib.request.OpenerDirector:
    # ProxyHandler({}) ignores http(s)_proxy: cameras are on the LAN.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       urllib.request.HTTPSHandler(context=_ssl_context()),
                                       _NoRedirect())


class Client:
    """One authenticated connection to a Reolink camera, NVR or Home Hub."""

    def __init__(self, host: str, username: str, password: str, port: int | None = None,
                 use_https: bool | None = None, timeout: float = 10.0):
        self.host = host.strip()
        self.username = username
        self.password = password
        self.port = port
        self.use_https = use_https
        self.timeout = timeout
        self._token: str | None = None
        self._lease_until = 0.0
        self._login_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._opener = _opener()

    # ------------------------------------------------------------------ URLs
    @property
    def scheme(self) -> str:
        return "https" if self.use_https is not False else "http"

    @property
    def effective_port(self) -> int:
        if self.port:
            return int(self.port)
        return 443 if self.use_https is not False else 80

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        default = 443 if self.scheme == "https" else 80
        port = "" if self.effective_port == default else f":{self.effective_port}"
        return f"{self.scheme}://{host}{port}"

    @property
    def api_url(self) -> str:
        return f"{self.base_url}/cgi-bin/api.cgi"

    @property
    def token(self) -> str | None:
        return self._token

    def url_with_token(self, params: dict[str, Any]) -> str:
        """A ``api.cgi`` URL carrying the session token (for mpv playback)."""
        self.ensure_login()
        query = dict(params)
        query["token"] = self._token or ""
        # Keep "/" literal in file names, as the official clients do.
        return f"{self.api_url}?{urllib.parse.urlencode(query, safe='/', quote_via=urllib.parse.quote)}"

    # ------------------------------------------------------------------ transport
    def _open(self, url: str, data: bytes | None = None, timeout: float | None = None):
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        req.add_header("User-Agent", "ReolinkLinux")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            return self._opener.open(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (300, 301, 302, 303, 307, 308):
                raise ConnectionFailed(f"{self.host}: the camera redirected the request "
                                       f"(HTTP {exc.code}); try HTTPS") from exc
            raise ConnectionFailed(f"{self.host}: HTTP error {exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError,
                OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ConnectionFailed(f"{self.host}:{self.effective_port}: {reason}") from exc

    def _post(self, body: list[dict], query: dict[str, str]) -> list[dict]:
        url = f"{self.api_url}?{urllib.parse.urlencode(query)}"
        payload = json.dumps(body).encode()
        with self._send_lock:
            with self._open(url, payload) as resp:
                raw = resp.read()
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            snippet = raw[:120].decode("utf-8", "replace").strip()
            raise ConnectionFailed(f"{self.host}: unexpected answer from the camera: {snippet!r}") from exc
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ConnectionFailed(f"{self.host}: unexpected answer from the camera")
        return data

    # ------------------------------------------------------------------ session
    def login(self) -> None:
        """Log in, trying HTTPS then HTTP when the scheme/port is not fixed yet."""
        with self._login_lock:
            self._login_locked()

    def _login_locked(self) -> None:
        if self.use_https is None:
            attempts = [(True, self.port or 443), (False, self.port or 80)]
            last: Exception | None = None
            for https, port in attempts:
                self.use_https, saved_port = https, self.port
                self.port = port
                try:
                    self._do_login()
                    return
                except AuthError:
                    raise
                except ReolinkError as exc:
                    last = exc
                    self.port = saved_port
            self.use_https = None
            raise ConnectionFailed(
                f"Could not reach the Reolink API on {self.host} (HTTPS or HTTP). Check the address, "
                f"and that HTTP/HTTPS is enabled on the camera. ({last})")
        self._do_login()

    def _do_login(self) -> None:
        body = [{"cmd": "Login", "action": 0,
                 "param": {"User": {"Version": "0", "userName": self.username, "password": self.password}}}]
        data = self._post(body, {"cmd": "Login", "token": "null"})
        item = data[0] if data else {}
        if item.get("code") != 0:
            err = item.get("error", {})
            detail = str(err.get("detail", ""))
            code = err.get("rspCode")
            if any(d in detail.lower() for d in _CRED_DETAILS) or code in (-7, -27):
                if "locked" in detail.lower():
                    raise AuthError(f"{self.host}: the camera locked logins after too many wrong passwords; "
                                    f"wait a few minutes and try again")
                raise AuthError(f"{self.host}: wrong user name or password")
            if code == -5:
                raise ApiError("Login", code, "too many open sessions; close other apps or wait a minute")
            raise ApiError("Login", code, detail)
        token = item["value"]["Token"]
        self._token = str(token["name"])
        self._lease_until = time.monotonic() + float(token.get("leaseTime", 3600))

    def ensure_login(self) -> None:
        if self._token and time.monotonic() < self._lease_until - TOKEN_MARGIN:
            return
        with self._login_lock:
            if self._token and time.monotonic() < self._lease_until - TOKEN_MARGIN:
                return
            self._login_locked()

    def logout(self) -> None:
        if not self._token:
            return
        try:
            self._post([{"cmd": "Logout", "action": 0, "param": {}}], {"cmd": "Logout", "token": self._token})
        except ReolinkError:
            pass
        self._token = None
        self._lease_until = 0.0

    def expire(self) -> None:
        self._token = None
        self._lease_until = 0.0

    # ------------------------------------------------------------------ commands
    def execute(self, commands: list[dict], retry: bool = True) -> list[dict]:
        """Send a batch of commands; returns one result dict per command, in order.

        Per-command failures are returned (``code != 0``), not raised; a session that
        expired is renewed once transparently.
        """
        results: list[dict] = []
        for start in range(0, len(commands), MAX_BATCH):
            chunk = commands[start:start + MAX_BATCH]
            results.extend(self._execute_chunk(chunk, retry))
        return results

    def _execute_chunk(self, chunk: list[dict], retry: bool) -> list[dict]:
        self.ensure_login()
        first = chunk[0].get("cmd", "") if chunk else ""
        data = self._post(chunk, {"cmd": first, "token": self._token or ""})
        if retry and any(_needs_login(item) for item in data):
            self.expire()
            self.ensure_login()
            data = self._post(chunk, {"cmd": first, "token": self._token or ""})
        if len(data) != len(chunk):
            # Some firmware drops answers for unknown commands; align by command name.
            by_cmd: dict[str, list[dict]] = {}
            for item in data:
                by_cmd.setdefault(item.get("cmd", ""), []).append(item)
            aligned = []
            for cmd in chunk:
                lst = by_cmd.get(cmd.get("cmd", ""), [])
                aligned.append(lst.pop(0) if lst else {"cmd": cmd.get("cmd"), "code": 1,
                                                       "error": {"rspCode": -9, "detail": "no answer"}})
            data = aligned
        return data

    def command(self, cmd: str, param: dict | None = None, action: int = 0) -> dict:
        """Run one command and return its ``value`` (raises ApiError on failure)."""
        item = self.execute([{"cmd": cmd, "action": action, "param": param or {}}])[0]
        check(item)
        return item.get("value", {}) or {}

    def command_full(self, cmd: str, param: dict | None = None, action: int = 0) -> dict:
        """Like :meth:`command` but returns the whole item (``value`` and ``range``)."""
        item = self.execute([{"cmd": cmd, "action": action, "param": param or {}}])[0]
        check(item)
        return item

    # ------------------------------------------------------------------ binary transfers
    def get_bytes(self, params: dict[str, Any], timeout: float | None = None) -> bytes:
        """GET ``api.cgi`` with query parameters and return the body (e.g. ``Snap``)."""
        self.ensure_login()
        for attempt in (0, 1):
            query = dict(params)
            query["token"] = self._token or ""
            url = f"{self.api_url}?{urllib.parse.urlencode(query)}"
            with self._open(url, timeout=timeout) as resp:
                ctype = resp.headers.get("Content-Type", "")
                data = resp.read()
            if "json" in ctype or "text" in ctype or data[:1] in (b"[", b"{"):
                try:
                    item = json.loads(data.decode("utf-8", "replace"))
                    item = item[0] if isinstance(item, list) and item else item
                except ValueError:
                    item = {}
                if attempt == 0 and _needs_login(item):
                    self.expire()
                    self.ensure_login()
                    continue
                check(item if isinstance(item, dict) else {}, params.get("cmd", "request"))
            return data
        raise ConnectionFailed(f"{self.host}: request failed")

    def download(self, params: dict[str, Any], dest, progress: Callable[[int, int], None] | None = None,
                 cancelled: Callable[[], bool] | None = None, chunk_size: int = 256 * 1024) -> int:
        """Stream a file (``cmd=Download``) into the open binary file ``dest``.

        Returns the number of bytes written. ``progress(done, total)`` is called as
        data arrives (``total`` is 0 if the camera does not send a length).
        """
        self.ensure_login()
        query = dict(params)
        query["token"] = self._token or ""
        url = f"{self.api_url}?{urllib.parse.urlencode(query)}"
        with self._open(url, timeout=max(self.timeout, 30)) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if ctype.startswith("text/html") or "json" in ctype:
                body = resp.read(4096)
                try:
                    item = json.loads(body.decode("utf-8", "replace"))
                    item = item[0] if isinstance(item, list) and item else item
                except ValueError:
                    item = {}
                check(item if isinstance(item, dict) else {}, params.get("cmd", "Download"))
                raise ApiError(params.get("cmd", "Download"), None, "the camera returned no file")
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancelled and cancelled():
                    raise Cancelled("download cancelled")
                block = resp.read(chunk_size)
                if not block:
                    break
                dest.write(block)
                done += len(block)
                if progress:
                    progress(done, total)
        return done


def _needs_login(item: dict) -> bool:
    if not isinstance(item, dict) or item.get("code") == 0:
        return False
    err = item.get("error", {}) or {}
    return err.get("rspCode") in (-6, -21) or "please login first" in str(err.get("detail", "")).lower()


def check(item: dict, command: str | None = None) -> None:
    """Raise ApiError if a command result is a failure."""
    if not isinstance(item, dict):
        raise ApiError(command or "command", None, "unexpected answer")
    if item.get("code", 0) == 0:
        value = item.get("value")
        if isinstance(value, dict) and "rspCode" in value and value.get("rspCode") not in (200, 0):
            raise ApiError(item.get("cmd", command or "command"), value.get("rspCode"), str(value.get("detail", "")))
        return
    err = item.get("error", {}) or {}
    raise ApiError(item.get("cmd", command or "command"), err.get("rspCode"), str(err.get("detail", "")))


def ok(item: dict) -> bool:
    return isinstance(item, dict) and item.get("code") == 0
