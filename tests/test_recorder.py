import shutil
import time

import pytest

from reolinklinux.core import recorder


def test_command_for_rtsp_copies_video():
    cmd = recorder.build_command("rtsp://u:p@cam/h265Preview_01_main", "/tmp/x.mp4")
    assert "-rtsp_transport" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
    assert "+frag_keyframe+empty_moov+default_base_moof" in cmd
    assert cmd[-1] == "/tmp/x.mp4"


def test_command_for_segments():
    cmd = recorder.build_command("https://cam/flv?x", "/tmp/r_%Y.mkv", "mkv", 600)
    assert cmd[cmd.index("-f") + 1] == "segment"
    assert cmd[cmd.index("-segment_format") + 1] == "matroska"
    assert "-rw_timeout" in cmd


def test_safe_name():
    assert recorder.safe_name("Front/Door: 1") == "Front_Door_ 1"
    assert recorder.safe_name("..") == "camera"


def test_cleanup_old_files(tmp_path):
    old = tmp_path / "a.mp4"
    new = tmp_path / "b.mp4"
    old.write_bytes(b"x")
    new.write_bytes(b"y")
    past = time.time() - 10 * 86400
    import os
    os.utime(old, (past, past))
    assert recorder.cleanup_old_files(tmp_path, 7) == 1
    assert not old.exists() and new.exists()
    assert recorder.cleanup_old_files(tmp_path, 0) == 0


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_records_a_synthetic_stream(tmp_path):
    rec = recorder.Recorder("av://lavfi:testsrc2=size=320x240:rate=15", tmp_path, "Test Cam")
    rec.start()
    time.sleep(3)
    rec.stop()
    assert rec.files and rec.files[0].exists() and rec.files[0].stat().st_size > 1000
    assert rec.files[0].name.startswith("Test Cam_")
