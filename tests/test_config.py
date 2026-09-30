import os
import stat

from reolinklinux.core.config import CameraConfig, Config, default_video_dir


def test_roundtrip_and_permissions(tmp_path):
    path = tmp_path / "c.json"
    cfg = Config(path)
    cam = CameraConfig(name="Garden", host="192.168.1.20", username="admin")
    cfg.add(cam, "pw")
    cfg.settings.grid_quality = "main"
    cfg.save()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = Config(path)
    assert again.cameras[0].name == "Garden"
    assert again.password(again.cameras[0]) == "pw"      # keyring disabled in tests: stored in the file
    assert again.settings.grid_quality == "main"


def test_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"settings": {"theme": "dark", "bogus": 1}, "cameras": [{"host": "h", "future": 2}]}')
    cfg = Config(path)
    assert cfg.settings.theme == "dark"
    assert cfg.cameras[0].host == "h"


def test_move_and_remove(tmp_path):
    cfg = Config(tmp_path / "c.json")
    a, b = CameraConfig(name="A", host="a"), CameraConfig(name="B", host="b")
    cfg.add(a, "")
    cfg.add(b, "")
    cfg.move(b.id, -1)
    assert [c.name for c in cfg.cameras] == ["B", "A"]
    cfg.remove(b.id)
    assert [c.name for c in cfg.cameras] == ["A"]


def test_default_dirs_follow_xdg(tmp_path, monkeypatch):
    conf = tmp_path / "config"
    conf.mkdir(exist_ok=True)
    (conf / "user-dirs.dirs").write_text('XDG_VIDEOS_DIR="$HOME/Filme"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(conf))
    assert default_video_dir().parts[-2:] == ("Filme", "ReolinkLinux")


def test_old_defaults_are_migrated(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"version": 1, "settings": {"hwdec": "auto-safe", "grid_quality": "sub", "theme": "dark"}}')
    cfg = Config(path)
    assert cfg.settings.hwdec == "auto-copy-safe" and cfg.settings.grid_quality == "auto"
    cfg.settings.grid_quality = "sub"                       # a later, deliberate choice sticks
    cfg.save()
    assert Config(path).settings.grid_quality == "sub"
