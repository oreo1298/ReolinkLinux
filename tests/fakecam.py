"""A fake Reolink camera for tests: the CGI/JSON API over HTTP and an RTSP DESCRIBE endpoint.

Responses follow the structure real firmware sends (Reolink CGI API v8 and captures
from TrackMix / Duo 2 / RLC-811A / NVR firmware as documented by reolink_aio).
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import socketserver
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER, PASSWORD = "admin", "s3cret!&pw"


def _ab(ver: int = 1, permit: int = 6) -> dict:
    return {"permit": permit, "ver": ver}


PROFILES = {
    "trackmix": {
        "dev": {"model": "Reolink TrackMix PoE", "name": "Garden", "hardVer": "IPC_560SD88MP", "firmVer":
                "v3.0.0.3429_2404222054", "serial": "00000000000000", "channelNum": 1, "type": "IPC",
                "exactType": "IPC", "buildDay": "build 24042220"},
        "chn": {"ptzType": _ab(7), "ptzPreset": _ab(1), "ptzCtrl": _ab(1), "supportAutoTrackStream": _ab(1),
                "aiTrack": _ab(1), "GetPtzGuard": _ab(1), "supportPtzCheck": _ab(1), "supportPtzSpeed": _ab(1),
                "supportDigitalZoom": _ab(1), "ledControl": _ab(1), "floodLight": _ab(1), "alarmAudio": _ab(1),
                "recReplay": _ab(1), "mainEncType": _ab(1)},
        "enc": ({"width": 3840, "height": 2160, "frameRate": 25, "bitRate": 8192, "vType": "h265"},
                {"width": 896, "height": 512, "frameRate": 15, "bitRate": 512, "vType": "h264"}),
    },
    "duo2": {
        "dev": {"model": "Reolink Duo 2 PoE", "name": "Driveway", "hardVer": "IPC_529SD78MP", "firmVer":
                "v3.0.0.2356_23062000", "serial": "11111111111111", "channelNum": 1, "type": "IPC",
                "exactType": "IPC"},
        "chn": {"ptzType": _ab(0), "ledControl": _ab(1), "floodLight": _ab(1), "supportAudioAlarm": _ab(1),
                "recReplay": _ab(1), "mainEncType": _ab(1)},
        "enc": ({"size": "4608*1728", "frameRate": 20, "bitRate": 6144, "vType": "h265"},
                {"size": "1536*576", "frameRate": 15, "bitRate": 512, "vType": "h264"}),
    },
    "nvr": {
        "dev": {"model": "RLN8-410", "name": "NVR", "hardVer": "N3MB01", "firmVer": "v3.3.0.226_23031644",
                "serial": "22222222222222", "channelNum": 8, "type": "NVR", "exactType": "NVR"},
        "chn": {"ptzType": _ab(0), "recReplay": _ab(1)},
        "enc": ({"width": 2560, "height": 1440, "frameRate": 20, "bitRate": 4096, "vType": "h264"},
                {"width": 640, "height": 360, "frameRate": 10, "bitRate": 256, "vType": "h264"}),
    },
}


class FakeCamera:
    def __init__(self, profile: str = "trackmix", password: str = PASSWORD):
        self.profile = PROFILES[profile]
        self.kind = profile
        self.password = password
        self.tokens: set[str] = set()
        self.logins = 0
        self.commands: list[dict] = []
        self.presets = {1: "Gate", 3: "Patio"}
        self.ir = "Auto"
        self.spot = {"state": 0, "bright": 70, "mode": 1}
        self.expire_next = False
        self.lock = threading.Lock()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.http.daemon_threads = True
        self.port = self.http.server_address[1]
        self.rtsp = _RtspServer(("127.0.0.1", 0), self)
        self.rtsp_port = self.rtsp.server_address[1]
        self.rtsp_paths = {"/h265Preview_01_main": "H265", "/h264Preview_01_sub": "H264",
                           "/Preview_01_autotrack": "H265", "/h264Preview_02_sub": "H264"}
        for t in (threading.Thread(target=self.http.serve_forever, daemon=True),
                  threading.Thread(target=self.rtsp.serve_forever, daemon=True)):
            t.start()

    def close(self) -> None:
        self.http.shutdown()
        self.rtsp.shutdown()
        self.http.server_close()
        self.rtsp.server_close()

    # ------------------------------------------------------------------ API
    def handle(self, query: dict, body: list | None) -> tuple[int, str, bytes]:
        cmd = query.get("cmd", "")
        token = query.get("token", "")
        if cmd == "Login":
            user = body[0]["param"]["User"]
            if user["userName"] != USER or user["password"] != self.password:
                return 200, "text/html", json.dumps([{"cmd": "Login", "code": 1, "error": {
                    "detail": "login failed", "rspCode": -7}}]).encode()
            self.logins += 1
            tok = f"tok{self.logins}"
            self.tokens.add(tok)
            return 200, "text/html", json.dumps([{"cmd": "Login", "code": 0, "value": {
                "Token": {"leaseTime": 3600, "name": tok}}}]).encode()
        if self.expire_next:
            self.expire_next = False
            self.tokens.clear()
        if token not in self.tokens:
            return 200, "text/html", json.dumps([{"cmd": cmd, "code": 1, "error": {
                "detail": "please login first", "rspCode": -6}}]).encode()
        if cmd == "Snap":
            return 200, "image/jpeg", b"\xff\xd8\xff\xe0JPEGDATA" + (b"T" if query.get("iLogicChannel") else b"W")
        if cmd == "Download":
            data = b"MP4" * 50000
            return 200, "application/octet-stream", data
        out = []
        for item in body or []:
            with self.lock:
                self.commands.append(item)
            out.append(self.answer(item))
        return 200, "text/html", json.dumps(out).encode()

    def answer(self, item: dict) -> dict:
        cmd = item.get("cmd")
        param = item.get("param", {}) or {}
        ch = param.get("channel", 0)
        prof = self.profile
        nvr = self.kind == "nvr"

        def ok(value: dict, rng: dict | None = None) -> dict:
            out = {"cmd": cmd, "code": 0, "value": value}
            if rng is not None:
                out["range"] = rng
            return out

        def err(code: int = -9) -> dict:
            return {"cmd": cmd, "code": 1, "error": {"detail": "not support", "rspCode": code}}

        chn = prof["chn"]
        if cmd == "GetDevInfo":
            return ok({"DevInfo": prof["dev"]})
        if cmd == "GetChannelstatus":
            if not nvr:
                return err()
            status = [{"channel": i, "name": f"Cam {i + 1}", "online": 1 if i < 3 else 0,
                       "typeInfo": "RLC-811A" if i != 1 else "Reolink TrackMix PoE"} for i in range(8)]
            return ok({"count": 8, "status": status})
        if cmd == "GetAbility":
            n = 8 if nvr else 1
            return ok({"Ability": {"GetWhiteLed": _ab(1), "rtsp": _ab(3), "abilityChn": [dict(chn) for _ in range(n)]}})
        if cmd == "GetNetPort":
            return ok({"NetPort": {"httpEnable": 1, "httpPort": 80, "httpsEnable": 1, "httpsPort": 443,
                                   "mediaPort": 9000, "onvifEnable": 1, "onvifPort": 8000, "rtmpEnable": 1,
                                   "rtmpPort": 1935, "rtspEnable": 1, "rtspPort": self.rtsp_port}})
        if cmd == "GetLocalLink":
            return ok({"LocalLink": {"mac": "ec:71:db:12:34:56"}})
        if cmd == "GetP2p":
            return ok({"P2p": {"uid": "95270000ABCDEFGH"}})
        if cmd == "GetHddInfo":
            return ok({"HddInfo": [{"capacity": 122070, "size": 61035, "mount": 1, "format": 1,
                                    "number": 0, "storageType": 2}]})
        if cmd == "GetTime":
            return ok({"Time": {"year": 2026, "mon": 9, "day": 30, "hour": 12, "min": 0, "sec": 0,
                                "timeZone": 0, "timeFmt": "DD/MM/YYYY", "hourFmt": 0}})
        if cmd == "SetTime":
            return ok({"rspCode": 200})
        if cmd == "GetEnc":
            main, sub = prof["enc"]
            return ok({"Enc": {"channel": ch, "audio": 1, "mainStream": main, "subStream": sub}})
        if cmd == "GetRtspUrl":
            if nvr:
                return err()
            return ok({"rtspUrl": {"channel": ch, "mainStream": f"rtsp://127.0.0.1:{self.rtsp_port}/h265Preview_01_main",
                                   "subStream": f"rtsp://127.0.0.1:{self.rtsp_port}/h264Preview_01_sub"}})
        if cmd == "GetOsd":
            return ok({"Osd": {"channel": ch, "osdChannel": {"name": f"Osd {ch}"}}})
        if cmd == "GetZoomFocus":
            if chn.get("ptzType", {}).get("ver") in (0, None) and "supportDigitalZoom" not in chn:
                return err()
            return ok({"ZoomFocus": {"channel": ch, "zoom": {"pos": 5}, "focus": {"pos": 10}}},
                      {"ZoomFocus": {"zoom": {"pos": {"min": 0, "max": 33}}, "focus": {"pos": {"min": 0, "max": 223}}}})
        if cmd == "GetPtzPreset":
            presets = [{"channel": ch, "enable": 1 if i in self.presets else 0, "id": i,
                        "name": self.presets.get(i, f"pos{i}")} for i in range(1, 65)]
            return ok({"PtzPreset": presets})
        if cmd == "SetPtzPreset":
            p = param["PtzPreset"]
            if p.get("enable"):
                self.presets[int(p["id"])] = p.get("name", "")
            else:
                self.presets.pop(int(p["id"]), None)
            return ok({"rspCode": 200})
        if cmd == "GetPtzPatrol":
            return ok({"PtzPatrol": [{"channel": ch, "enable": 1, "id": 0, "name": "Tour", "running": 0},
                                     {"channel": ch, "enable": 0, "id": 1, "name": "", "running": 0}]})
        if cmd == "GetPtzGuard":
            return ok({"PtzGuard": {"benable": 1, "bexistPos": 1, "channel": ch, "timeout": 60}})
        if cmd == "GetAiCfg":
            return ok({"channel": ch, "aiTrack": 0, "bSmartTrack": 1, "aiDisappearBackTime": 10}, {"aiTrack": [0, 1]})
        if cmd == "GetAutoFocus":
            return err()
        if cmd == "GetIrLights":
            return ok({"IrLights": {"channel": ch, "state": self.ir}})
        if cmd == "SetIrLights":
            self.ir = param["IrLights"]["state"]
            return ok({"rspCode": 200})
        if cmd == "GetWhiteLed":
            return ok({"WhiteLed": {"channel": ch, **self.spot}})
        if cmd == "SetWhiteLed":
            self.spot.update({k: v for k, v in param["WhiteLed"].items() if k != "channel"})
            return ok({"rspCode": 200})
        if cmd == "GetMdState":
            return ok({"state": 1})
        if cmd == "GetAiState":
            return ok({"channel": ch, "people": {"alarm_state": 1, "support": 1},
                       "vehicle": {"alarm_state": 0, "support": 1}, "dog_cat": {"alarm_state": 0, "support": 1},
                       "face": {"alarm_state": 0, "support": 0}})
        if cmd in ("PtzCtrl", "StartZoomFocus", "SetAiCfg", "AudioAlarmPlay", "SetPtzGuard", "PtzCheck",
                   "SetAutoFocus", "Reboot"):
            return ok({"rspCode": 200})
        if cmd == "Search":
            s = param["Search"]
            if s["onlyStatus"]:
                return ok({"SearchResult": {"channel": s["channel"], "Status": [
                    {"mon": s["StartTime"]["mon"], "year": s["StartTime"]["year"], "table": "0" * 4 + "1" + "0" * 20 + "11"}]}})
            day = s["StartTime"]
            files = [
                {"StartTime": {**day, "hour": 8, "min": 1, "sec": 2}, "EndTime": {**day, "hour": 8, "min": 3, "sec": 0},
                 "name": "Mp4Record/2026-09-30/RecS07_20260930_080102_080300_0_A714C0A000_21E67C.mp4",
                 "size": "2156000", "type": s["streamType"], "frameRate": 0, "width": 0, "height": 0},
                {"StartTime": {**day, "hour": 9, "min": 0, "sec": 0}, "EndTime": {**day, "hour": 9, "min": 1, "sec": 0},
                 "name": "Mp4Record/2026-09-30/RecM01_20260930_090000_090100_6D28808_1A468F9.mp4",
                 "size": "1000", "type": s["streamType"]},
            ]
            if s["StartTime"].get("hour", 0) > 8:
                files = [f for f in files if f["StartTime"]["hour"] >= s["StartTime"]["hour"]]
            return ok({"SearchResult": {"channel": s["channel"], "Status": [{"mon": day["mon"], "year": day["year"],
                                                                               "table": "1" * 30}], "File": files}})
        if cmd == "NvrDownload":
            return ok({"fileList": [{"fileName": "prepared/Rec_1.mp4", "fileSize": "150000"}]})
        return err(-24)

    def _handler(self):
        cam = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self, body):
                parts = urllib.parse.urlsplit(self.path)
                if parts.path != "/cgi-bin/api.cgi":
                    self.send_error(404)
                    return
                query = dict(urllib.parse.parse_qsl(parts.query))
                status, ctype, data = cam.handle(query, body)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                if ctype == "application/octet-stream":
                    self.send_header("Content-Disposition", f'attachment; filename="{query.get("output", "f.mp4")}"')
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._serve(None)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"[]")
                self._serve(body)

        return Handler


class _RtspServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    realm, nonce = "BC Streaming Media", "abcdef123456"

    def __init__(self, addr, cam: FakeCamera):
        self.cam = cam
        super().__init__(addr, _RtspHandler)


class _RtspHandler(socketserver.StreamRequestHandler):
    def handle(self):
        srv: _RtspServer = self.server
        while True:
            lines = []
            while True:
                line = self.rfile.readline()
                if not line:
                    return
                if line in (b"\r\n", b"\n"):
                    break
                lines.append(line.decode().rstrip("\r\n"))
            if not lines:
                return
            method, uri, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
            cseq = headers.get("cseq", "1")
            auth = headers.get("authorization", "")
            path = urllib.parse.urlsplit(uri).path
            if not auth:
                self.wfile.write((f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\nWWW-Authenticate: Digest "
                                  f'realm="{srv.realm}", nonce="{srv.nonce}"\r\n'
                                  f'WWW-Authenticate: Basic realm="{srv.realm}"\r\n\r\n').encode())
                continue
            params = dict(re.findall(r'(\w+)="([^"]*)"', auth))
            ha1 = hashlib.md5(f"{USER}:{srv.realm}:{srv.cam.password}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{method}:{params.get('uri', '')}".encode()).hexdigest()
            good = params.get("response") == hashlib.md5(f"{ha1}:{srv.nonce}:{ha2}".encode()).hexdigest()
            if not good:
                self.wfile.write(f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n\r\n".encode())
                return
            codec = srv.cam.rtsp_paths.get(path)
            if not codec:
                self.wfile.write(f"RTSP/1.0 404 Not Found\r\nCSeq: {cseq}\r\n\r\n".encode())
                return
            sdp = (f"v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 {codec}/90000\r\n"
                   "m=audio 0 RTP/AVP 97\r\na=rtpmap:97 MPEG4-GENERIC/16000\r\n")
            self.wfile.write((f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Type: application/sdp\r\n"
                              f"Content-Length: {len(sdp)}\r\n\r\n{sdp}").encode())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
