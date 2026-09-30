from reolinklinux.core import rtsp


def test_describe_with_digest_auth(trackmix):
    from fakecam import PASSWORD, USER
    url = f"rtsp://127.0.0.1:{trackmix.rtsp_port}/h265Preview_01_main"
    res = rtsp.describe(url, USER, PASSWORD)
    assert res.ok and res.codec == "h265" and res.has_audio


def test_describe_wrong_password_and_missing_path(trackmix):
    from fakecam import USER
    base = f"rtsp://127.0.0.1:{trackmix.rtsp_port}"
    assert rtsp.describe(base + "/h265Preview_01_main", USER, "wrong").status == 401
    from fakecam import PASSWORD
    assert rtsp.describe(base + "/nothing", USER, PASSWORD).status == 404


def test_describe_credentials_from_url(trackmix):
    url = f"rtsp://admin:s3cret%21%26pw@127.0.0.1:{trackmix.rtsp_port}/h264Preview_01_sub"
    assert rtsp.describe(url).codec == "h264"


def test_describe_unreachable():
    from fakecam import free_port
    res = rtsp.describe(f"rtsp://127.0.0.1:{free_port()}/x", "a", "b", timeout=1)
    assert res.status == 0 and not res.ok
