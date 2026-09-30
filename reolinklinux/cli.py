"""``reolinkctl``: command-line control of Reolink cameras (scripts, cron jobs, SSH).

Cameras are addressed by a saved name from the GUI (``-c Driveway``) or directly with
``--host``/``--user``/``--password`` (the password can also come from $REOLINK_PASSWORD).
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import sys
import time
from pathlib import Path

from . import __app_name__, __version__
from .core import discovery, recorder
from .core.config import Config
from .core.demo import DemoDevice
from .core.device import PTZ_OPS, Device
from .core.errors import ReolinkError
from .core.models import MAIN, SUB, TELE, WIDE


def _device(args) -> Device:
    if args.host:
        password = args.password if args.password is not None else os.environ.get("REOLINK_PASSWORD")
        if password is None:
            password = getpass.getpass(f"Password for {args.user}@{args.host}: ")
        https = {"https": True, "http": False}.get(args.scheme)
        return Device(args.host, args.user, password, args.port, https)
    cfg = Config(Path(args.config)) if args.config else Config()
    if not cfg.cameras:
        raise SystemExit("No saved cameras. Use --host, or add cameras in the ReolinkLinux app.")
    wanted = (args.camera or "").lower()
    matches = [c for c in cfg.cameras if not wanted or c.label.lower() == wanted or c.host == args.camera]
    if not matches:
        matches = [c for c in cfg.cameras if wanted in c.label.lower()]
    if not matches:
        raise SystemExit(f"No saved camera called {args.camera!r}. Try: reolinkctl list")
    if len(matches) > 1 and not wanted:
        names = ", ".join(c.label for c in matches)
        raise SystemExit(f"Several cameras are saved ({names}); choose one with -c NAME")
    c = matches[0]
    if c.demo:
        return DemoDevice(c.demo)
    return Device(c.host, c.username, cfg.password(c), c.port, c.https)


def _connected(args, probe: bool = False) -> Device:
    dev = _device(args)
    dev.connect(probe_streams=probe)
    return dev


def _lens(args) -> int:
    return TELE if getattr(args, "tele", False) else WIDE


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------- commands
def cmd_list(args) -> int:
    cfg = Config(Path(args.config)) if args.config else Config()
    if not cfg.cameras:
        print("No saved cameras.")
        return 0
    for c in cfg.cameras:
        where = "demo" if c.demo else c.host
        print(f"{c.label:<24} {where:<18} user={c.username}" + ("  [continuous]" if c.continuous_record else ""))
    return 0


def cmd_discover(args) -> int:
    items = discovery.discover(scan=not args.no_scan)
    if not items:
        print("No devices found.")
        return 1
    for it in items:
        api = ("https" if it.https else "http") if it.api else "API off"
        print(f"{it.host:<16} {it.description:<40} {api}")
    return 0


def cmd_info(args) -> int:
    dev = _connected(args, probe=True)
    info = dev.info
    if args.json:
        _print_json({"device": info.__dict__, "ports": dev.ports, "storage": dev.storage_summary(),
                     "channels": [{"index": ch.index, "name": ch.name, "model": ch.model, "online": ch.online,
                                   "main": ch.main.__dict__, "sub": ch.sub.__dict__, "caps": ch.caps.__dict__,
                                   "presets": [p.__dict__ for p in ch.presets]} for ch in dev.channels]})
        dev.disconnect()
        return 0
    print(f"{info.model}  “{info.name}”  ({'NVR' if dev.is_nvr else 'camera'})")
    print(f"  firmware {info.firmware}, hardware {info.hardware}, serial {info.serial}")
    print(f"  address  {dev.client.base_url}   uid {info.uid or '—'}   mac {info.mac or '—'}")
    print(f"  storage  {dev.storage_summary()}")
    p = dev.ports
    print(f"  ports    RTSP {p['rtsp']}{'' if p['rtsp_enabled'] else ' (off)'}, "
          f"RTMP {p['rtmp']}{'' if p['rtmp_enabled'] else ' (off)'}, ONVIF {p['onvif']}")
    for ch in dev.channels:
        c = ch.caps
        feats = [n for f, n in ((c.pan_tilt, "pan/tilt"), (c.optical_zoom, "zoom"), (c.focus, "focus"),
                                (c.presets, f"presets({len(ch.presets)})"), (c.patrol, "patrol"),
                                (c.auto_track, "auto-track"), (c.telephoto, "telephoto"), (c.ir_lights, "IR"),
                                (c.spotlight, "spotlight"), (c.siren, "siren")) if f]
        print(f"\n  channel {ch.index}: {ch.name} ({ch.model or info.model}){'' if ch.online else ' OFFLINE'}")
        print(f"    clear  {ch.main.resolution} {ch.main.codec} {ch.main.fps} fps {ch.main.bitrate} kbps")
        print(f"    fluent {ch.sub.resolution} {ch.sub.codec} {ch.sub.fps} fps")
        print(f"    features: {', '.join(feats) or 'none'}")
        print(f"    AI: {', '.join(c.ai_types) or 'motion only'}")
        if ch.online:
            for lens in ch.lenses:
                for q in (MAIN, SUB):
                    url = dev.stream_url(ch.index, lens, q)
                    print(f"    {('tele ' if lens == TELE else '') + q:<10} {_hide(url)}")
    dev.disconnect()
    return 0


def _hide(url: str) -> str:
    import re
    return re.sub(r"(rtsp://[^:/@]+:)[^@]*@", r"\1***@", re.sub(r"password=[^&]*", "password=***", url))


def cmd_stream_url(args) -> int:
    dev = _connected(args)
    url = dev.probe_stream(args.channel, _lens(args), SUB if args.sub else MAIN, "flv" if args.flv else "rtsp")
    print(url if args.show_password else _hide(url))
    dev.disconnect()
    return 0


def cmd_snapshot(args) -> int:
    dev = _connected(args)
    data = dev.snapshot(args.channel, _lens(args))
    out = Path(args.output or f"{(dev.channel(args.channel).name or 'snapshot').replace(' ', '_')}_"
                              f"{dt.datetime.now():%Y-%m-%d_%H-%M-%S}.jpg")
    out.write_bytes(data)
    print(f"Saved {out} ({len(data) // 1024} KB)")
    dev.disconnect()
    return 0


def cmd_ptz(args) -> int:
    dev = _connected(args)
    op = {o.lower(): o for o in PTZ_OPS}.get(args.op.lower().replace("-", "").replace("_", ""))
    if op is None:
        raise SystemExit(f"Unknown operation {args.op}; use one of: {', '.join(PTZ_OPS)}")
    dev.ptz(args.channel, op, speed=args.speed)
    if op not in ("Stop", "Auto") and args.duration > 0:
        time.sleep(args.duration)
        dev.ptz_stop(args.channel)
    dev.disconnect()
    return 0


def cmd_preset(args) -> int:
    dev = _connected(args)
    ch = dev.channel(args.channel)
    if args.action == "list":
        for p in ch.presets:
            print(f"{p.id:>3}  {p.name}")
        if not ch.presets:
            print("No presets.")
    elif args.action == "goto":
        dev.goto_preset(args.channel, _preset_id(ch, args.value), args.speed)
    elif args.action == "save":
        name = args.name or f"Preset {args.value}"
        dev.save_preset(args.channel, int(args.value) if args.value else dev.free_preset_id(args.channel), name)
    elif args.action == "delete":
        dev.delete_preset(args.channel, _preset_id(ch, args.value))
    dev.disconnect()
    return 0


def _preset_id(ch, value) -> int:
    if value is None:
        raise SystemExit("Give a preset id or name")
    if str(value).isdigit():
        return int(value)
    for p in ch.presets:
        if p.name.lower() == str(value).lower():
            return p.id
    raise SystemExit(f"No preset called {value!r}")


def cmd_zoom(args) -> int:
    dev = _connected(args)
    dev.set_zoom(args.channel, args.position)
    dev.disconnect()
    return 0


def cmd_light(args) -> int:
    dev = _connected(args)
    if args.kind == "ir":
        dev.set_ir(args.channel, {"auto": "Auto", "on": "On", "off": "Off"}[args.state])
    else:
        dev.set_spotlight(args.channel, on=args.state == "on", brightness=args.brightness)
    dev.disconnect()
    return 0


def cmd_siren(args) -> int:
    dev = _connected(args)
    dev.siren(args.channel, not args.stop, args.times)
    dev.disconnect()
    return 0


def cmd_track(args) -> int:
    dev = _connected(args)
    dev.set_auto_track(args.channel, args.state == "on")
    dev.disconnect()
    return 0


def cmd_recordings(args) -> int:
    dev = _connected(args)
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    recs = dev.recordings(args.channel, day, SUB if args.sub else MAIN, _lens(args))
    if args.json:
        _print_json([{**r.__dict__, "triggers": r.triggers.labels()} for r in recs])
    else:
        for r in recs:
            print(f"{r.start:%H:%M:%S}–{r.end:%H:%M:%S}  {r.size / 1e6:7.1f} MB  "
                  f"{', '.join(r.triggers.labels()) or '-':<24} {r.name}")
        print(f"{len(recs)} recordings on {day}")
    dev.disconnect()
    return 0


def cmd_download(args) -> int:
    dev = _connected(args)
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    recs = dev.recordings(args.channel, day, SUB if args.sub else MAIN, _lens(args))
    if args.name:
        recs = [r for r in recs if args.name in r.name or r.start.strftime("%H:%M:%S").startswith(args.name)]
    elif not args.all:
        raise SystemExit("Choose recordings with --name (file name or start time) or --all")
    if not recs:
        print("Nothing to download.")
        return 1
    folder = Path(args.output or ".")
    label = dev.channel(args.channel).name or dev.info.name or "camera"
    for r in recs:
        path = folder / r.local_filename(label)
        if path.exists():
            print(f"skip {path} (exists)")
            continue

        def progress(done, total, name=path.name):
            pct = f"{done * 100 // total:3d}%" if total else f"{done // 1024} KB"
            print(f"\r{name}: {pct}", end="", flush=True)

        dev.download(r, str(path), progress)
        print(f"\r{path}: done{' ' * 10}")
    dev.disconnect()
    return 0


def cmd_record(args) -> int:
    dev = _connected(args)
    url = dev.probe_stream(args.channel, _lens(args), SUB if args.sub else MAIN)
    label = (dev.channel(args.channel).name or dev.info.name or "camera") + (" tele" if args.tele else "")
    folder = Path(args.output or ".")
    rec = recorder.Recorder(url, folder, label, args.format, segment_minutes=args.segment)
    rec.start()
    print(f"Recording {label} to {folder.resolve()} — press Ctrl+C to stop")
    try:
        end = time.monotonic() + args.duration if args.duration else None
        while end is None or time.monotonic() < end:
            time.sleep(0.5)
            if not rec.running:
                break
    except KeyboardInterrupt:
        pass
    rec.stop()
    for f in rec.files:
        if f.exists():
            print(f"Saved {f} ({f.stat().st_size / 1e6:.1f} MB)")
    if rec.error:
        print(f"ffmpeg: {rec.error}", file=sys.stderr)
    return 0


def cmd_sync_clock(args) -> int:
    dev = _connected(args)
    before = dev.clock_offset()
    dev.sync_clock()
    print(f"Camera clock set to {dev.clock:%Y-%m-%d %H:%M:%S}"
          + (f" (was {before:+.0f} s off)" if before is not None else ""))
    dev.disconnect()
    return 0


def cmd_reboot(args) -> int:
    dev = _connected(args)
    dev.reboot()
    print(f"{dev.info.name or dev.host} is rebooting")
    return 0


# ---------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reolinkctl", description=f"{__app_name__} command-line tool for Reolink cameras")
    p.add_argument("--version", action="version", version=f"reolinkctl ({__app_name__}) {__version__}")
    conn = p.add_argument_group("camera")
    conn.add_argument("-c", "--camera", help="a camera saved in the ReolinkLinux app (name or address)")
    conn.add_argument("--host", help="camera address, instead of a saved camera")
    conn.add_argument("--user", default="admin")
    conn.add_argument("--password", help="password (default: $REOLINK_PASSWORD, or ask)")
    conn.add_argument("--port", type=int)
    conn.add_argument("--scheme", choices=("auto", "https", "http"), default="auto")
    conn.add_argument("--config", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def cmd(name, fn, help_text, channel=True, lens=False):
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(func=fn)
        if channel:
            sp.add_argument("--channel", "--ch", type=int, default=0, help="channel (NVRs; default 0)")
        if lens:
            sp.add_argument("--tele", action="store_true", help="telephoto lens (TrackMix)")
        return sp

    cmd("list", cmd_list, "list the cameras saved in the app", channel=False)
    d = cmd("discover", cmd_discover, "search the local network for Reolink devices", channel=False)
    d.add_argument("--no-scan", action="store_true", help="ONVIF discovery only, no subnet scan")
    i = cmd("info", cmd_info, "device details, capabilities and stream URLs", channel=False)
    i.add_argument("--json", action="store_true")
    s = cmd("stream-url", cmd_stream_url, "print the working RTSP (or FLV) URL", lens=True)
    s.add_argument("--sub", action="store_true", help="fluent (sub) stream")
    s.add_argument("--flv", action="store_true", help="FLV over HTTP(S) instead of RTSP")
    s.add_argument("--show-password", action="store_true")
    sn = cmd("snapshot", cmd_snapshot, "save a full-resolution JPEG", lens=True)
    sn.add_argument("-o", "--output")
    pt = cmd("ptz", cmd_ptz, "move the camera: left, right, up, down, leftup, …, zoominc, zoomdec, auto, stop")
    pt.add_argument("op")
    pt.add_argument("--speed", type=int, default=32)
    pt.add_argument("--duration", type=float, default=0.5, help="seconds to move before stopping (default 0.5)")
    pr = cmd("preset", cmd_preset, "list, go to, save or delete PTZ presets")
    pr.add_argument("action", choices=("list", "goto", "save", "delete"))
    pr.add_argument("value", nargs="?", help="preset id (or name for goto/delete)")
    pr.add_argument("--name", help="name when saving")
    pr.add_argument("--speed", type=int)
    z = cmd("zoom", cmd_zoom, "set the optical zoom position")
    z.add_argument("position", type=int)
    li = cmd("light", cmd_light, "infrared LEDs (auto/on/off) or spotlight (on/off)")
    li.add_argument("kind", choices=("ir", "spotlight"))
    li.add_argument("state", choices=("auto", "on", "off"))
    li.add_argument("--brightness", type=int)
    si = cmd("siren", cmd_siren, "sound (or stop) the siren")
    si.add_argument("--times", type=int, default=1)
    si.add_argument("--stop", action="store_true")
    tr = cmd("track", cmd_track, "switch auto tracking on or off")
    tr.add_argument("state", choices=("on", "off"))
    rc = cmd("recordings", cmd_recordings, "list recordings on the SD card / NVR for a day", lens=True)
    rc.add_argument("--date", help="YYYY-MM-DD (default today)")
    rc.add_argument("--sub", action="store_true")
    rc.add_argument("--json", action="store_true")
    dl = cmd("download", cmd_download, "download recordings to this PC", lens=True)
    dl.add_argument("--date", help="YYYY-MM-DD (default today)")
    dl.add_argument("--name", help="file name or start time (HH:MM[:SS]) of the recording")
    dl.add_argument("--all", action="store_true", help="every recording of the day")
    dl.add_argument("--sub", action="store_true")
    dl.add_argument("-o", "--output", help="folder (default: current)")
    rr = cmd("record", cmd_record, "record the live stream to this PC with FFmpeg", lens=True)
    rr.add_argument("--duration", type=float, default=0, help="seconds (default: until Ctrl+C)")
    rr.add_argument("--segment", type=int, default=0, help="split into files of N minutes")
    rr.add_argument("--format", choices=("mp4", "mkv"), default="mp4")
    rr.add_argument("--sub", action="store_true")
    rr.add_argument("-o", "--output", help="folder (default: current)")
    cmd("sync-clock", cmd_sync_clock, "set the camera clock to this PC's time", channel=False)
    cmd("reboot", cmd_reboot, "reboot the camera or NVR", channel=False)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ReolinkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
