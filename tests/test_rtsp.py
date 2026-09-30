import pytest

from reolinklinux.core import rtsp


def url(cam, path="/h265Preview_01_main"):
    return f"rtsp://127.0.0.1:{cam.rtsp_port}{path}"


@pytest.mark.parametrize("mode,scheme", [("digest", "digest"), ("qop", "digest"), ("basic", "basic"),
                                         ("close", "digest")])
def test_describe_answers_every_challenge_style(trackmix, mode, scheme):
    from fakecam import PASSWORD, USER
    trackmix.rtsp_auth = mode
    res = rtsp.describe(url(trackmix), USER, PASSWORD)
    assert res.ok, res.describe()
    assert res.codec == "h265" and res.has_audio and res.auth == scheme


def test_digest_rejected_falls_back_to_basic(trackmix):
    """Some firmware advertises Digest but only accepts Basic."""
    from fakecam import PASSWORD, USER
    trackmix.rtsp_auth = "digest"
    original = rtsp.digest_header
    try:
        rtsp.digest_header = lambda *a, **k: 'Digest username="x", response="bad"'
        res = rtsp.describe(url(trackmix), USER, PASSWORD)
    finally:
        rtsp.digest_header = original
    assert res.ok and res.auth == "basic"


def test_wrong_password_missing_path_and_rejection(trackmix):
    from fakecam import PASSWORD, USER
    assert rtsp.describe(url(trackmix), USER, "wrong").status == 401
    assert rtsp.describe(url(trackmix, "/nothing"), USER, PASSWORD).status == 404
    trackmix.rtsp_auth = "reject"
    res = rtsp.describe(url(trackmix), USER, PASSWORD)
    assert res.status == 401 and not res.ok and len(res.challenges) == 2


def test_describe_credentials_from_url(trackmix):
    u = f"rtsp://admin:s3cret%21%26pw@127.0.0.1:{trackmix.rtsp_port}/h264Preview_01_sub"
    assert rtsp.describe(u).codec == "h264"


def test_describe_unreachable():
    from fakecam import free_port
    res = rtsp.describe(f"rtsp://127.0.0.1:{free_port()}/x", "a", "b", timeout=1)
    assert res.status == 0 and not res.ok and res.refused


def test_digest_header_with_qop_matches_rfc2617():
    header = rtsp.digest_header("Mufasa", "Circle Of Life", "GET", "/dir/index.html",
                                {"realm": "testrealm@host.com", "nonce": "dcd98b7102dd2f0e8b11d0f600bfb0c093",
                                 "qop": "auth,auth-int", "opaque": "5ccc069c403ebaf9f0171e9517f40e41"},
                                nc=1, cnonce="0a4f113b")
    assert 'response="6629fae49393a05397450978507c4ef1"' in header      # the RFC 2617 example
    assert 'opaque="5ccc069c403ebaf9f0171e9517f40e41"' in header and "qop=auth" in header
