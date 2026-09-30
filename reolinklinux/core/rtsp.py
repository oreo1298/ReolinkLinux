"""A minimal RTSP client used to check which stream URL a camera accepts.

Reolink firmware generations name their RTSP paths differently
(``h264Preview_01_main``, ``h265Preview_01_main``, ``Preview_01_main``, …). Sending an
authenticated ``DESCRIBE`` takes a few milliseconds on a LAN and tells us whether a
path exists and which codec it carries, without starting a video decoder.
"""

from __future__ import annotations

import base64
import hashlib
import re
import socket
import urllib.parse
from dataclasses import dataclass


@dataclass
class DescribeResult:
    status: int
    reason: str = ""
    codec: str = ""        # "h264" / "h265" when the SDP says so
    has_audio: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200


def _read_response(sock: socket.socket) -> tuple[int, str, dict[str, str], bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            break
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "replace").split("\r\n")
    m = re.match(r"RTSP/\d\.\d\s+(\d+)\s*(.*)", lines[0] if lines else "")
    if not m:
        raise ConnectionError("not an RTSP server")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            # Keep the first occurrence: cameras list the Digest challenge before Basic.
            headers.setdefault(k.strip().lower(), v.strip())
    length = int(headers.get("content-length", "0") or 0)
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return int(m.group(1)), m.group(2).strip(), headers, body


def _auth_params(header: str) -> dict[str, str]:
    return {k.lower(): v for k, v in re.findall(r'(\w+)="?([^",]*)"?', header)}


def _digest(username: str, password: str, method: str, uri: str, params: dict[str, str]) -> str:
    realm, nonce = params.get("realm", ""), params.get("nonce", "")
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}"')


def describe(url: str, username: str = "", password: str = "", timeout: float = 4.0) -> DescribeResult:
    """Send DESCRIBE (answering Basic or Digest challenges); never raises for HTTP-like errors."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 554
    username = username or urllib.parse.unquote(parts.username or "")
    password = password or urllib.parse.unquote(parts.password or "")
    netloc = f"[{host}]" if ":" in host else host
    if parts.port:
        netloc += f":{parts.port}"
    clean = urllib.parse.urlunsplit(("rtsp", netloc, parts.path, parts.query, ""))
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            cseq = 1
            auth = ""
            for _ in range(3):
                req = (f"DESCRIBE {clean} RTSP/1.0\r\nCSeq: {cseq}\r\nAccept: application/sdp\r\n"
                       f"User-Agent: ReolinkLinux\r\n{auth}\r\n")
                sock.sendall(req.encode())
                status, reason, headers, body = _read_response(sock)
                cseq += 1
                if status == 401 and not auth and "www-authenticate" in headers:
                    challenge = headers["www-authenticate"]
                    if challenge.lower().startswith("digest"):
                        auth = "Authorization: " + _digest(username, password, "DESCRIBE", clean,
                                                           _auth_params(challenge)) + "\r\n"
                    else:
                        token = base64.b64encode(f"{username}:{password}".encode()).decode()
                        auth = f"Authorization: Basic {token}\r\n"
                    continue
                break
    except (OSError, ConnectionError, ValueError) as exc:
        return DescribeResult(0, str(exc))
    sdp = body.decode("latin-1", "replace")
    codec = ""
    if re.search(r"a=rtpmap:\d+\s+H265/", sdp, re.I):
        codec = "h265"
    elif re.search(r"a=rtpmap:\d+\s+H264/", sdp, re.I):
        codec = "h264"
    return DescribeResult(status, reason, codec, "m=audio" in sdp)
