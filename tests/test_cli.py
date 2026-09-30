import json

from reolinklinux.cli import main


def args_for(cam):
    from fakecam import PASSWORD
    return ["--host", "127.0.0.1", "--port", str(cam.port), "--scheme", "http", "--password", PASSWORD]


def test_info_json(trackmix, capsys):
    assert main(args_for(trackmix) + ["info", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["device"]["model"] == "Reolink TrackMix PoE"
    assert data["channels"][0]["caps"]["telephoto"] is True


def test_info_text_hides_password(trackmix, capsys):
    assert main(args_for(trackmix) + ["info"]) == 0
    out = capsys.readouterr().out
    assert "s3cret" not in out and "***@" in out


def test_ptz_preset_light_and_track(trackmix, capsys):
    base = args_for(trackmix)
    assert main(base + ["ptz", "left", "--duration", "0"]) == 0
    assert main(base + ["preset", "list"]) == 0
    assert "Gate" in capsys.readouterr().out
    assert main(base + ["preset", "goto", "Patio"]) == 0
    assert main(base + ["light", "ir", "off"]) == 0
    assert main(base + ["track", "off"]) == 0
    ops = [(c["cmd"], c["param"].get("op")) for c in trackmix.commands]
    assert ("PtzCtrl", "Left") in ops and ("PtzCtrl", "ToPos") in ops
    assert trackmix.ir == "Off"


def test_recordings_and_download(trackmix, tmp_path, capsys):
    base = args_for(trackmix)
    assert main(base + ["recordings", "--date", "2026-09-30"]) == 0
    assert "2 recordings" in capsys.readouterr().out
    assert main(base + ["download", "--date", "2026-09-30", "--name", "08:01", "-o", str(tmp_path)]) == 0
    assert len(list(tmp_path.glob("*.mp4"))) == 1


def test_snapshot(trackmix, tmp_path):
    out = tmp_path / "s.jpg"
    assert main(args_for(trackmix) + ["snapshot", "-o", str(out)]) == 0
    assert out.read_bytes().startswith(b"\xff\xd8")


def test_bad_password_exit_code(trackmix, capsys):
    rc = main(["--host", "127.0.0.1", "--port", str(trackmix.port), "--scheme", "http", "--password", "x", "info"])
    assert rc == 2
    assert "wrong user name or password" in capsys.readouterr().err


def test_list_saved_cameras(capsys):
    from reolinklinux.core.config import CameraConfig, Config
    cfg = Config()
    cfg.add(CameraConfig(name="Garden", host="10.0.0.5"), "pw")
    assert main(["list"]) == 0
    assert "Garden" in capsys.readouterr().out
