# ReolinkLinux — repository notes for Claude

A Linux desktop client for Reolink PoE cameras / NVRs / Home Hubs: live view (mpv),
PTZ, lights, SD-card playback and downloads, local recording (FFmpeg), plus the
`reolinkctl` CLI. Python ≥ 3.10; PySide6 for the GUI; primary target Arch Linux.

## Layout
- `reolinklinux/core/` — stdlib-only camera logic (no Qt imports here):
  `api.py` (HTTP/JSON client: token login, HTTPS→HTTP fallback, batching ≤30 cmds,
  auto re-login on rspCode -6), `device.py` (capabilities from GetAbility + Get* answers,
  stream URL candidates + RTSP DESCRIBE probing, PTZ/lights/siren, Search/Download),
  `rtsp.py` (DESCRIBE with Digest/Basic), `recordings.py` (file-name event flags),
  `recorder.py` (ffmpeg stream-copy recorder with reconnect + segments), `discovery.py`
  (WS-Discovery + /24 scan of port 9000), `config.py` (JSON config, keyring), `demo.py`
  (simulated cameras; video from mpv/ffmpeg `lavfi`).
- `reolinklinux/gui/` — PySide6 app. `mpv.py` is our own ctypes binding (client + OpenGL
  render API); `video.py` draws mpv into QOpenGLWidget; `live.py` (camera list + video
  wall), `controls.py` (PTZ/lights/device panel), `playback.py` (calendar, timeline,
  downloads), `manager.py` (CameraManager / RecordingManager / DownloadManager),
  `worker.py` (thread pool + ordered SerialQueue per camera).
- `reolinklinux/cli.py` — `reolinkctl`.
- `tests/fakecam.py` — fake camera (HTTP API + RTSP server with digest auth) used by the tests.

## Invariants / gotchas
- Never call libmpv from its wakeup/update callbacks: they only emit Qt signals, which
  must stay `Qt.QueuedConnection` (mpv invokes them synchronously inside render calls).
- On software OpenGL (llvmpipe etc.) mpv's full renderer draws black/garbled frames
  intermittently; `video._use_simple_renderer` switches to `gpu-dumb-mode`. Keep it.
- PTZ "move" and "stop" must reach the camera in order: use `CameraManager.control`
  (per-camera SerialQueue), not the shared pool.
- Worker results come back through `worker.run(fn, done, error)`; callbacks run on the
  GUI thread. Don't touch Qt objects inside `fn`.
- TrackMix telephoto lens: on its own it is stream channel index+1 (`Preview_02_main`,
  `Snap/Search channel=1`); behind an NVR/Home Hub it is the channel's autotrack stream
  (`Preview_0N_autotrack`, `channelN_autotrack_*.bcs`, `iLogicChannel: 1`). `Device._tele_forms`
  / `_tele_candidates` try the likely form first and fall back to the other.
- Stream choice (`Device.probe_stream`): an unconfirmed RTSP URL beats FLV; FLV only for H.264
  and only when RTSP is off / every path 404s. Reolink can't send H.265 over FLV. Probes run one
  connection at a time per camera. `probe_log` feeds `core/diagnose.py` (`reolinkctl diagnose`,
  Device tab → Diagnostics…) — ask users for that report when video misbehaves.
- Default `hwdec` is `auto-copy-safe`: zero-copy interop garbled a real Duo 2 (coloured dots and
  lines over the picture). Per-camera `software_decode` is the escape hatch. Config migrations live
  in `config._migrate` (bump `CONFIG_VERSION`).
- Credentials never go into logs or error text: see `_clean_error` / `_redact` / `_hide`.
- The GUI shares the EZP2019Linux / FirmwareLab design system (`theme.py`, `icons.py`,
  `widgets.py`); keep it visually consistent with those apps.

## Running
```sh
python -m venv --system-site-packages .venv && . .venv/bin/activate
pip install -e ".[dev]"
make test            # QT_QPA_PLATFORM=offscreen pytest
make lint            # ruff
./reolinklinux.sh --demo
```
Headless screenshots: run under `Xvfb :99` with `DISPLAY=:99` (needs libmpv, ffmpeg,
Mesa); grab with `QScreen.grabWindow(0)` — `QWidget.grab()` re-renders GL widgets.
