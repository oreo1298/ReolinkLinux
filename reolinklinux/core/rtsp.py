"""A minimal RTSP client used to check which stream URL a camera accepts.

Reolink firmware generations name their RTSP paths differently
(``h264Preview_01_main``, ``h265Preview_01_main``, ``Preview_01_main``, …). Sending an
authenticated ``DESCRIBE`` takes a few milliseconds on a LAN and tells us whether a
path exists and which codec it carries, without starting a video decoder.

Authentication follows what cameras offer: Digest (RFC 2069 or RFC 2617 with ``qop``,
``opaque`` and MD5 / SHA-256), falling back to Basic. If the camera closes the
connection after its challenge, the answer is sent on a new connection.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import urllib.parse
from dataclasses import dataclass, field
from urllib.request import parse_http_list

_HASHES = {"MD5": hashlib.md5, "MD5-SESS": hashlib.md5, "SHA-256": hashlib.sha256, "SHA-256-SESS": hashlib.sha256}


@dataclass
class DescribeResult:
    status: int                 # RTSP status, or 0 when no answer was received
    reason: str = ""
    codec: str = ""             # "h264" / "h265" when the SDP says so
    has_audio: bool = False
    auth: str = ""              # "digest" / "basic" / "" (the scheme that was accepted)
    challenges: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def refused(self) -> bool:
        return self.status == 0 and "refused" in self.reason.lower()

    def describe(self) -> str:
        if self.ok:
            return f"200 OK ({self.codec or 'unknown codec'}{', audio' if self.has_audio else ''}, {self.auth or 'no'} auth)"
        if self.status:
            return f"{self.status} {self.reason}".strip()
        return f"no answer: {self.reason}"


def _read_response(sock: socket.socket) -> tuple[int, str, list[tuple[str, str]], bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            break
    if not buf:
        raise ConnectionResetError("the camera closed the connection")
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "replace").split("\r\n")
    m = re.match(r"RTSP/\d\.\d\s+(\d+)\s*(.*)", lines[0] if lines else "")
    if not m:
        raise ConnectionError("not an RTSP server")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers.append((k.strip().lower(), v.strip()))
    length = int(next((v for k, v in headers if k == "content-length"), "0") or 0)
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return int(m.group(1)), m.group(2).strip(), headers, body


def _params(challenge: str) -> dict[str, str]:
    out = {}
    for item in parse_http_list(challenge.split(" ", 1)[1] if " " in challenge else ""):
        if "=" in item:
            k, v = item.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] == '"':
                v = v[1:-1]
            out[k.strip().lower()] = v
    return out


def digest_header(username: str, password: str, method: str, uri: str, challenge: dict[str, str],
                  nc: int = 1, cnonce: str | None = None) -> str:
    algorithm = challenge.get("algorithm", "MD5").upper()
    hash_fn = _HASHES.get(algorithm, hashlib.md5)

    def h(text: str) -> str:
        return hash_fn(text.encode()).hexdigest()

    realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
    cnonce = cnonce or os.urandom(8).hex()
    ha1 = h(f"{username}:{realm}:{password}")
    if algorithm.endswith("-SESS"):
        ha1 = h(f"{ha1}:{nonce}:{cnonce}")
    ha2 = h(f"{method}:{uri}")
    qops = [q.strip() for q in challenge.get("qop", "").split(",") if q.strip()]
    fields = {"username": username, "realm": realm, "nonce": nonce, "uri": uri}
    if "auth" in qops:
        ncs = f"{nc:08x}"
        fields["response"] = h(f"{ha1}:{nonce}:{ncs}:{cnonce}:auth:{ha2}")
        extra = f', qop=auth, nc={ncs}, cnonce="{cnonce}"'
    else:
        fields["response"] = h(f"{ha1}:{nonce}:{ha2}")
        extra = ""
    if "algorithm" in challenge:
        extra += f", algorithm={challenge['algorithm']}"
    if "opaque" in challenge:
        fields["opaque"] = challenge["opaque"]
    return "Digest " + ", ".join(f'{k}="{v}"' for k, v in fields.items()) + extra


def describe(url: str, username: str = "", password: str = "", timeout: float = 5.0) -> DescribeResult:
    """Send DESCRIBE, answering the camera's authentication challenge. Never raises."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 554
    username = username or urllib.parse.unquote(parts.username or "")
    password = password or urllib.parse.unquote(parts.password or "")
    netloc = f"[{host}]" if ":" in host else host
    if parts.port:
        netloc += f":{parts.port}"
    clean = urllib.parse.urlunsplit(("rtsp", netloc, parts.path, parts.query, ""))

    sock: socket.socket | None = None
    cseq = 0

    def request(auth_line: str) -> tuple[int, str, list[tuple[str, str]], bytes]:
        nonlocal sock, cseq
        for attempt in (0, 1):
            if sock is None:
                sock = socket.create_connection((host, port), timeout=timeout)
                sock.settimeout(timeout)
            cseq += 1
            req = (f"DESCRIBE {clean} RTSP/1.0\r\nCSeq: {cseq}\r\nAccept: application/sdp\r\n"
                   f"User-Agent: ReolinkLinux\r\n{auth_line}\r\n")
            try:
                sock.sendall(req.encode())
                return _read_response(sock)
            except (ConnectionResetError, BrokenPipeError):
                # Some servers close the connection after a 401: answer on a new one.
                sock.close()
                sock = None
                if attempt:
                    raise
        raise ConnectionError("unreachable")

    challenges: list[str] = []
    used = ""
    try:
        status, reason, headers, body = request("")
        if status == 401:
            challenges = [v for k, v in headers if k == "www-authenticate"]
            digest = next((c for c in challenges if c.lower().startswith("digest")), None)
            basic = next((c for c in challenges if c.lower().startswith("basic")), None)
            attempts = []
            if digest:
                attempts.append(("digest", "Authorization: " + digest_header(
                    username, password, "DESCRIBE", clean, _params(digest)) + "\r\n"))
            if basic or not digest:
                token = base64.b64encode(f"{username}:{password}".encode()).decode()
                attempts.append(("basic", f"Authorization: Basic {token}\r\n"))
            for scheme, line in attempts:
                status, reason, headers, body = request(line)
                if status != 401:
                    used = scheme
                    break
    except (OSError, ConnectionError, ValueError) as exc:
        return DescribeResult(0, str(exc) or type(exc).__name__, challenges=challenges)
    finally:
        if sock is not None:
            sock.close()
    sdp = body.decode("latin-1", "replace")
    codec = ""
    if re.search(r"a=rtpmap:\d+\s+H265/", sdp, re.I):
        codec = "h265"
    elif re.search(r"a=rtpmap:\d+\s+H264/", sdp, re.I):
        codec = "h264"
    return DescribeResult(status, reason, codec, "m=audio" in sdp, used if status == 200 else "", challenges)
