"""Find Reolink devices on the local network.

Two methods run together:
  * ONVIF WS-Discovery (multicast probe to 239.255.255.250:3702), answered by cameras
    with ONVIF enabled;
  * a quick scan of the local /24 subnet for Reolink's media port (9000, always open)
    and the HTTP(S) API, which also finds cameras whose ONVIF service is off.
Each hit is then checked against ``/cgi-bin/api.cgi`` to confirm it is a Reolink API.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

WSD_ADDR = ("239.255.255.250", 3702)
PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header><w:MessageID>uuid:{id}</w:MessageID>
 <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
 <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
 <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>"""


@dataclass
class Found:
    host: str
    name: str = ""
    model: str = ""
    https: bool | None = None
    port: int | None = None
    sources: set[str] = field(default_factory=set)
    api: bool = False        # the Reolink HTTP(S) API answered

    @property
    def description(self) -> str:
        bits = [b for b in (self.name, self.model) if b]
        return " · ".join(bits) if bits else "Reolink device"


def local_ipv4() -> str | None:
    """The address of the interface used for the default route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))      # TEST-NET, nothing is sent
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def ws_discovery(timeout: float = 2.5) -> dict[str, Found]:
    found: dict[str, Found] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.3)
        sock.bind(("", 0))
        for _ in range(2):
            sock.sendto(PROBE.format(id=uuid.uuid4()).encode(), WSD_ADDR)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            text = data.decode("utf-8", "replace")
            xaddrs = re.findall(r"https?://([0-9.]+)(?::\d+)?/onvif", text)
            host = xaddrs[0] if xaddrs else addr[0]
            scopes = " ".join(re.findall(r"<[^>]*Scopes[^>]*>([^<]*)<", text))
            name = re.search(r"onvif://www\.onvif\.org/name/([^\s<]+)", scopes)
            hw = re.search(r"onvif://www\.onvif\.org/hardware/([^\s<]+)", scopes)
            item = found.setdefault(host, Found(host))
            item.sources.add("onvif")
            if name:
                item.name = urllib.parse.unquote(name.group(1)).replace("_", " ")
            if hw:
                item.model = urllib.parse.unquote(hw.group(1))
    except OSError:
        pass
    finally:
        sock.close()
    return found


def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def subnet_scan(timeout: float = 0.35, workers: int = 96) -> dict[str, Found]:
    me = local_ipv4()
    if not me:
        return {}
    net = ipaddress.ip_network(f"{me}/24", strict=False)
    hosts = [str(h) for h in net.hosts() if str(h) != me]

    def check(host: str) -> Found | None:
        if _port_open(host, 9000, timeout):
            return Found(host, sources={"scan"})
        return None

    found: dict[str, Found] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item in pool.map(check, hosts):
            if item:
                found[item.host] = item
    return found


def probe_api(item: Found, timeout: float = 3.0) -> Found:
    """Confirm a Reolink API on HTTPS or HTTP (an unauthenticated call answers 'please login')."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    for https, port in ((True, 443), (False, 80)):
        url = f"{'https' if https else 'http'}://{item.host}:{port}/cgi-bin/api.cgi?cmd=GetDevInfo"
        try:
            with opener.open(url, timeout=timeout) as resp:
                body = resp.read(4096).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - any failure just means "not on this port"
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("cmd"):
            item.api, item.https, item.port = True, https, port
            break
    return item


def discover(timeout: float = 2.5, scan: bool = True) -> list[Found]:
    """Run WS-Discovery and the subnet scan in parallel, then confirm each device."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        wsd = pool.submit(ws_discovery, timeout)
        sub = pool.submit(subnet_scan) if scan else None
        found = wsd.result()
        if sub:
            for host, item in sub.result().items():
                found.setdefault(host, item).sources |= item.sources
    with ThreadPoolExecutor(max_workers=16) as pool:
        items = list(pool.map(probe_api, found.values()))
    # A port-9000 hit without a Reolink API is kept only if ONVIF also saw it.
    items = [i for i in items if i.api or "onvif" in i.sources]
    return sorted(items, key=lambda i: tuple(int(p) for p in i.host.split(".")) if i.host.count(".") == 3 else (999,))

