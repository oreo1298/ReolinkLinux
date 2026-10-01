from fakecam import PASSWORD, USER, flv_bytes

from reolinklinux.core import diagnose, flv
from reolinklinux.core.device import Device
from reolinklinux.core.models import MAIN, SUB, TELE, WIDE


def connect(cam, lavf, protocol="auto"):
    dev = Device("127.0.0.1", USER, PASSWORD, port=cam.port, use_https=False, timeout=5)
    dev.lavf, dev.protocol = lavf, protocol
    dev.connect()
    return dev


def serve_all(cam, main_form):
    cam.flv_streams = {"channel0_main.bcs": main_form, "channel0_sub.bcs": "h264",
                       "channel1_main.bcs": main_form, "channel1_sub.bcs": "h264"}


def test_parse_recognises_each_form():
    assert flv.parse(flv_bytes("h264")).codec == "h264"
    legacy, enhanced = flv.parse(flv_bytes("hevc")), flv.parse(flv_bytes("hvc1"))
    assert legacy.codec == enhanced.codec == "h265" and enhanced.enhanced and not legacy.enhanced
    assert legacy.has_audio
    assert not flv.parse(b'{"error": "login"}').ok
    assert not flv.parse(flv_bytes("h264")[:30]).ok             # video tag not received yet


def test_playable_needs_a_recent_ffmpeg_for_h265():
    legacy, enhanced = flv.parse(flv_bytes("hevc")), flv.parse(flv_bytes("hvc1"))
    assert flv.playable(flv.parse(flv_bytes("h264")), (0, 0))
    assert not flv.playable(legacy, (61, 7)) and flv.playable(legacy, (62, 3))      # FFmpeg 7.1 / 8.0
    assert not flv.playable(enhanced, (60, 3)) and flv.playable(enhanced, (60, 16))  # FFmpeg 6.0 / 6.1


def test_h265_over_flv_is_chosen_with_ffmpeg_8(trackmix):
    serve_all(trackmix, "hevc")
    dev = connect(trackmix, (62, 3))
    assert "stream=channel0_main.bcs" in dev.stream_url(0, WIDE, MAIN)
    assert "stream=channel1_main.bcs" in dev.stream_url(0, TELE, MAIN)       # telephoto lens
    assert "stream=channel0_sub.bcs" in dev.stream_url(0, WIDE, SUB)
    assert dev.stream_codec(0, WIDE, MAIN) == "h265" and dev.probe_log[(0, WIDE, MAIN)].confirmed
    cands = dev.stream_candidates(0, WIDE, MAIN)
    assert cands[0].startswith("http") and cands[1].startswith("rtsp://")   # RTSP stays as the fallback
    assert "/h265Preview_01_main" not in [p for p, _a in trackmix.rtsp_requests]


def test_h265_stays_on_rtsp_when_ffmpeg_cannot_read_it(trackmix):
    serve_all(trackmix, "hevc")
    dev = connect(trackmix, (61, 7))
    assert dev.stream_url(0, WIDE, MAIN).endswith("/h265Preview_01_main")
    assert not any(u.startswith("http") for u in dev.stream_candidates(0, WIDE, MAIN))
    assert "stream=channel0_sub.bcs" in dev.stream_url(0, WIDE, SUB)        # H.264 over FLV still fine
    text = diagnose.report(dev, run_ffprobe=False)
    assert "FLV channel0_main.bcs: FLV H265 (classic FLV), audio (too new for this FFmpeg)" in text
    assert PASSWORD not in text


def test_enhanced_flv_needs_only_ffmpeg_6_1(trackmix):
    serve_all(trackmix, "hvc1")
    assert "stream=channel0_main.bcs" in connect(trackmix, (60, 16)).stream_url(0, WIDE, MAIN)


def test_no_flv_for_h265_without_a_known_ffmpeg(trackmix):
    serve_all(trackmix, "hevc")
    dev = connect(trackmix, (0, 0))
    assert dev.stream_url(0, WIDE, MAIN).startswith("rtsp://")
    assert "channel0_main.bcs" not in trackmix.flv_requests                 # not even asked


def test_camera_without_flv_uses_rtsp(trackmix):
    dev = connect(trackmix, (62, 3))
    assert dev.stream_url(0, WIDE, MAIN).endswith("/h265Preview_01_main")
    assert dev.stream_url(0, WIDE, SUB).endswith("/h264Preview_01_sub")


def test_rtsp_setting_skips_flv(trackmix):
    serve_all(trackmix, "hevc")
    dev = connect(trackmix, (62, 3), protocol="rtsp")
    assert dev.stream_url(0, WIDE, MAIN).endswith("/h265Preview_01_main")
    assert trackmix.flv_requests == []


def test_rtmp_off_skips_flv(trackmix):
    serve_all(trackmix, "hevc")
    trackmix.rtmp_enabled = 0
    dev = connect(trackmix, (62, 3))
    assert dev.stream_url(0, WIDE, MAIN).endswith("/h265Preview_01_main")
    assert trackmix.flv_requests == []
