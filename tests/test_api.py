import pytest

from reolinklinux.core.api import Client
from reolinklinux.core.errors import ApiError, AuthError, ConnectionFailed


def client(cam, password=None):
    from fakecam import PASSWORD, USER
    return Client("127.0.0.1", USER, password or PASSWORD, port=cam.port, use_https=None, timeout=5)


def test_login_falls_back_from_https_to_http(trackmix):
    c = client(trackmix)
    c.login()
    assert c.use_https is False
    assert c.token == "tok1"
    assert c.base_url == f"http://127.0.0.1:{trackmix.port}"


def test_wrong_password_raises_auth_error(trackmix):
    c = client(trackmix, password="nope")
    with pytest.raises(AuthError):
        c.login()


def test_unreachable_host_raises_connection_failed():
    from fakecam import free_port
    c = Client("127.0.0.1", "admin", "x", port=free_port(), timeout=1)
    with pytest.raises(ConnectionFailed):
        c.login()


def test_expired_session_is_renewed_transparently(trackmix):
    c = client(trackmix)
    c.login()
    trackmix.expire_next = True
    value = c.command("GetDevInfo")
    assert value["DevInfo"]["model"] == "Reolink TrackMix PoE"
    assert c.token == "tok2"


def test_batches_are_split_and_keep_order(trackmix):
    c = client(trackmix)
    cmds = [{"cmd": "GetMdState", "action": 0, "param": {"channel": 0}} for _ in range(70)]
    cmds.append({"cmd": "GetDevInfo", "action": 0, "param": {}})
    res = c.execute(cmds)
    assert len(res) == 71
    assert res[-1]["cmd"] == "GetDevInfo"


def test_command_failure_raises_api_error(trackmix):
    c = client(trackmix)
    with pytest.raises(ApiError) as info:
        c.command("GetUnknownThing")
    assert info.value.rsp_code == -24
    assert info.value.unsupported


def test_binary_get_and_download(trackmix, tmp_path):
    c = client(trackmix)
    data = c.get_bytes({"cmd": "Snap", "channel": 0})
    assert data.startswith(b"\xff\xd8")
    seen = []
    with open(tmp_path / "f.mp4", "wb") as fh:
        size = c.download({"cmd": "Download", "source": "a/b c.mp4", "output": "b c.mp4"}, fh,
                          lambda d, t: seen.append((d, t)))
    assert size == 150000
    assert seen[-1] == (150000, 150000)


def test_url_with_token_encodes_file_names(trackmix):
    c = client(trackmix)
    url = c.url_with_token({"cmd": "Playback", "source": "Mp4Record/2026-09-30/Rec M.mp4"})
    assert "source=Mp4Record/2026-09-30/Rec%20M.mp4" in url
    assert url.endswith("token=tok1")
