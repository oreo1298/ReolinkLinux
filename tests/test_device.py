import datetime as dt
import os

import pytest

from reolinklinux.core.device import Device
from reolinklinux.core.models import MAIN, SUB, TELE, WIDE, Trigger


def connect(cam, probe=True):
    from fakecam import PASSWORD, USER
    dev = Device("127.0.0.1", USER, PASSWORD, port=cam.port, use_https=False, timeout=5)
    dev.connect(probe_streams=probe)
    return dev


def test_trackmix_capabilities(trackmix):
    dev = connect(trackmix)
    assert dev.info.model == "Reolink TrackMix PoE"
    assert not dev.is_nvr
    assert len(dev.channels) == 1
    ch = dev.channels[0]
    c = ch.caps
    assert c.pan_tilt and c.optical_zoom and c.presets and c.guard and c.calibrate and c.ptz_speed
    assert c.telephoto and c.auto_track and c.spotlight and c.ir_lights and c.siren and c.patrol
    assert not c.auto_focus
    assert ch.lenses == [WIDE, TELE]
    assert [p.name for p in ch.presets] == ["Gate", "Patio"]
    assert ch.patrols[0].name == "Tour"
    assert ch.auto_track_on is True and ch.auto_track_key == "bSmartTrack"
    assert (ch.zoom.zoom_min, ch.zoom.zoom_max, ch.zoom.zoom_pos) == (0, 33, 5)
    assert ch.main.resolution == "3840×2160" and ch.main.codec == "h265"
    assert ch.detection.motion and ch.detection.ai["people"] and ch.detection.active == ["people"]
    assert dev.info.mac == "ec:71:db:12:34:56" and dev.info.uid == "95270000ABCDEFGH"
    assert "SD card" in dev.storage_summary()


def test_duo2_is_a_fixed_panoramic_camera(duo2):
    dev = connect(duo2)
    ch = dev.channels[0]
    c = ch.caps
    assert not (c.pan_tilt or c.optical_zoom or c.presets or c.telephoto or c.auto_track)
    assert c.spotlight and c.ir_lights and c.siren
    assert ch.main.resolution == "4608×1728"   # parsed from "size"
    assert ch.lenses == [WIDE]


def test_nvr_channels(nvr):
    dev = connect(nvr, probe=False)
    assert dev.is_nvr
    assert [c.index for c in dev.channels] == list(range(8))
    assert [c.online for c in dev.channels][:4] == [True, True, True, False]
    assert dev.channels[1].model == "Reolink TrackMix PoE"
    assert dev.channels[0].name == "Cam 1"


def test_stream_probe_picks_working_urls(trackmix):
    dev = connect(trackmix)
    port = trackmix.rtsp_port
    main = dev.stream_url(0, WIDE, MAIN)
    assert main.endswith(f":{port}/h265Preview_01_main")
    assert "admin:s3cret%21%26pw@" in main          # password is URL-encoded
    assert dev.stream_url(0, WIDE, SUB).endswith("/h264Preview_01_sub")
    assert dev.stream_url(0, TELE, MAIN).endswith("/Preview_01_autotrack")
    assert dev.stream_url(0, TELE, SUB).endswith("/h264Preview_02_sub")
    assert dev.stream_codec(0, WIDE, MAIN) == "h265"


def test_stream_candidates_fallbacks(duo2):
    dev = connect(duo2, probe=False)
    cands = dev.stream_candidates(0, WIDE, MAIN)
    paths = [u.rsplit("/", 1)[-1] for u in cands[:-1]]
    assert paths == ["h265Preview_01_main", "Preview_01_main", "h264Preview_01_main"]
    assert cands[-1].startswith("http://") and "stream=channel0_main.bcs" in cands[-1]
    flv = dev.stream_candidates(0, WIDE, SUB, protocol="flv")
    assert "stream=channel0_sub.bcs" in flv[0]


def test_ptz_and_lights_send_the_right_commands(trackmix):
    dev = connect(trackmix, probe=False)
    trackmix.commands.clear()
    dev.ptz(0, "Left", speed=99)
    dev.ptz_stop(0)
    dev.goto_preset(0, 3)
    dev.set_zoom(0, 50)
    dev.set_ir(0, "Off")
    dev.set_spotlight(0, on=True, brightness=40)
    dev.siren(0, True, 2)
    dev.set_auto_track(0, False)
    sent = [(c["cmd"], c["param"]) for c in trackmix.commands]
    assert ("PtzCtrl", {"channel": 0, "op": "Left", "speed": 64}) in sent
    assert ("PtzCtrl", {"channel": 0, "op": "Stop"}) in sent
    assert ("PtzCtrl", {"channel": 0, "op": "ToPos", "id": 3}) in sent
    assert ("StartZoomFocus", {"ZoomFocus": {"channel": 0, "op": "ZoomPos", "pos": 33}}) in sent
    assert ("SetIrLights", {"IrLights": {"channel": 0, "state": "Off"}}) in sent
    assert ("SetWhiteLed", {"WhiteLed": {"channel": 0, "state": 1, "bright": 40}}) in sent
    assert ("AudioAlarmPlay", {"alarm_mode": "times", "manual_switch": 0, "times": 2, "channel": 0}) in sent
    assert ("SetAiCfg", {"channel": 0, "bSmartTrack": 0}) in sent
    assert trackmix.ir == "Off" and trackmix.spot["bright"] == 40
    with pytest.raises(ValueError):
        dev.ptz(0, "Sideways")


def test_presets_save_and_delete(trackmix):
    dev = connect(trackmix, probe=False)
    pid = dev.free_preset_id(0)
    assert pid == 2
    dev.save_preset(0, pid, "Shed")
    assert [p.name for p in dev.channels[0].presets] == ["Gate", "Shed", "Patio"]
    dev.delete_preset(0, 1)
    assert [p.id for p in dev.channels[0].presets] == [2, 3]


def test_poll_updates_detection_and_lights(trackmix):
    dev = connect(trackmix, probe=False)
    trackmix.ir = "On"
    dev.poll(lights=True)
    ch = dev.channels[0]
    assert ch.lights.ir_state == "On"
    assert ch.detection.ai["people"] is True


def test_recording_search_and_download(trackmix, tmp_path):
    dev = connect(trackmix, probe=False)
    day = dt.date(2026, 9, 30)
    assert dev.recording_days(0, 2026, 9) == {5, 26, 27}
    recs = dev.recordings(0, day)
    assert len(recs) == 2
    first = recs[0]
    assert first.start == dt.datetime(2026, 9, 30, 8, 1, 2) and first.duration.total_seconds() == 118
    assert first.triggers & Trigger.PERSON and first.size == 2156000
    tele = dev.recordings(0, day, lens=TELE)
    assert tele[0].lens == TELE
    search = [c for c in trackmix.commands if c["cmd"] == "Search"][-1]["param"]["Search"]
    assert search["iLogicChannel"] == 1 and search["streamType"] == "main"
    urls = dev.playback_urls(first)
    assert "cmd=Playback" in urls[0] and "cmd=Download" in urls[1] and "playback.bcs" in urls[2]
    path = tmp_path / first.local_filename("Garden")
    size = dev.download(first, str(path))
    assert size == 150000 and path.exists() and not os.path.exists(str(path) + ".part")


def test_nvr_download_prepares_file(nvr, tmp_path):
    dev = connect(nvr, probe=False)
    rec = dev.recordings(0, dt.date(2026, 9, 30))[0]
    dev.download(rec, str(tmp_path / "x.mp4"))
    assert any(c["cmd"] == "NvrDownload" for c in nvr.commands)


def test_snapshot_and_clock(trackmix):
    dev = connect(trackmix, probe=False)
    assert dev.snapshot(0).endswith(b"W")
    assert dev.snapshot(0, TELE).endswith(b"T")
    assert dev.clock == dt.datetime(2026, 9, 30, 12, 0, 0)
    dev.sync_clock(dt.datetime(2026, 10, 1, 7, 8, 9))
    sent = [c for c in trackmix.commands if c["cmd"] == "SetTime"][0]["param"]["Time"]
    assert (sent["year"], sent["mon"], sent["day"], sent["hour"], sent["min"], sent["sec"]) == (2026, 10, 1, 7, 8, 9)
    assert sent["timeFmt"] == "DD/MM/YYYY"      # other fields are preserved
