"""Exceptions raised by the camera client, with messages meant for people."""

from __future__ import annotations

# rspCode values documented in Reolink's CGI API guide.
RSP_CODES = {
    -1: "missing parameters",
    -2: "the camera ran out of memory",
    -3: "check error",
    -4: "invalid parameters",
    -5: "too many sessions are open on the camera",
    -6: "login required",
    -7: "login failed",
    -8: "the operation timed out",
    -9: "not supported by this camera",
    -10: "protocol error",
    -11: "failed to read",
    -12: "failed to get the configuration",
    -13: "failed to set the configuration",
    -16: "failed to send data",
    -17: "failed to receive data",
    -21: "the session token is not valid",
    -22: "a text value is too long",
    -24: "unknown command",
    -25: "internal error",
    -26: "not supported by this camera (ability error)",
    -27: "invalid user",
    -31: "the camera is busy",
}


class ReolinkError(Exception):
    """Base class for every error reported by the client."""


class ConnectionFailed(ReolinkError):
    """The camera could not be reached (network, TLS or HTTP-level failure)."""


class AuthError(ReolinkError):
    """Wrong user name or password, or the account is locked."""


class ApiError(ReolinkError):
    """The camera answered, but refused or failed the command."""

    def __init__(self, command: str, rsp_code: int | None = None, detail: str = ""):
        self.command = command
        self.rsp_code = rsp_code
        self.detail = detail
        text = detail or RSP_CODES.get(rsp_code or 0, "")
        if not text and rsp_code is not None:
            text = f"error {rsp_code}"
        super().__init__(f"{command}: {text or 'failed'}")

    @property
    def unsupported(self) -> bool:
        return self.rsp_code in (-9, -26, -24)


class NotSupported(ReolinkError):
    """The feature is not available on this camera or channel."""


class Cancelled(ReolinkError):
    """A long operation (such as a download) was cancelled by the user."""
