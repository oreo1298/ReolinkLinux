"""A small ctypes binding to libmpv: the client API plus the OpenGL render API.

Only what the video widget needs is bound. Writing it here (instead of depending on
python-mpv) keeps the dependency list to PySide6 + the libmpv shared library that
every distribution ships with its mpv package, and avoids API differences between
python-mpv releases.

Threading rules (from libmpv's documentation):
  * the wakeup and render-update callbacks run on mpv's own threads and must not call
    back into mpv; they only notify the GUI thread (see ``VideoWidget``).
  * all other calls here happen on the GUI thread.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from ctypes import CFUNCTYPE, POINTER, Structure, c_char_p, c_double, c_int, c_int64, c_uint64, c_ulong, c_void_p

# --- enums ---------------------------------------------------------------------

FORMAT_NONE = 0
FORMAT_STRING = 1
FORMAT_FLAG = 3
FORMAT_INT64 = 4
FORMAT_DOUBLE = 5

EVENT_NONE = 0
EVENT_SHUTDOWN = 1
EVENT_LOG_MESSAGE = 2
EVENT_START_FILE = 6
EVENT_END_FILE = 7
EVENT_FILE_LOADED = 8
EVENT_VIDEO_RECONFIG = 17
EVENT_SEEK = 20
EVENT_PLAYBACK_RESTART = 21
EVENT_PROPERTY_CHANGE = 22
EVENT_HOOK = 25

END_FILE_EOF = 0
END_FILE_STOP = 2
END_FILE_QUIT = 3
END_FILE_ERROR = 4
END_FILE_REDIRECT = 5

RENDER_PARAM_INVALID = 0
RENDER_PARAM_API_TYPE = 1
RENDER_PARAM_OPENGL_INIT_PARAMS = 2
RENDER_PARAM_OPENGL_FBO = 3
RENDER_PARAM_FLIP_Y = 4
RENDER_PARAM_BLOCK_FOR_TARGET_TIME = 12

# --- structures ------------------------------------------------------------------


class Event(Structure):
    _fields_ = [("event_id", c_int), ("error", c_int), ("reply_userdata", c_uint64), ("data", c_void_p)]


class EventProperty(Structure):
    _fields_ = [("name", c_char_p), ("format", c_int), ("data", c_void_p)]


class EventEndFile(Structure):
    _fields_ = [("reason", c_int), ("error", c_int)]


class EventLogMessage(Structure):
    _fields_ = [("prefix", c_char_p), ("level", c_char_p), ("text", c_char_p), ("log_level", c_int)]


class EventHook(Structure):
    _fields_ = [("name", c_char_p), ("id", c_uint64)]


class RenderParam(Structure):
    _fields_ = [("type", c_int), ("data", c_void_p)]


GetProcAddressFn = CFUNCTYPE(c_void_p, c_void_p, c_char_p)
CallbackFn = CFUNCTYPE(None, c_void_p)


class OpenGLInitParams(Structure):
    # ``extra_exts`` only exists in client API 1.x (ignored there, removed in 2.0);
    # keeping the trailing NULL field is harmless for both.
    _fields_ = [("get_proc_address", GetProcAddressFn), ("get_proc_address_ctx", c_void_p),
                ("extra_exts", c_char_p)]


class OpenGLFbo(Structure):
    _fields_ = [("fbo", c_int), ("w", c_int), ("h", c_int), ("internal_format", c_int)]


# --- library loading ---------------------------------------------------------------

_lib = None
_load_error = ""


def _candidates() -> list[str]:
    names = []
    env = os.environ.get("REOLINKLINUX_LIBMPV")
    if env:
        names.append(env)
    found = ctypes.util.find_library("mpv")
    if found:
        names.append(found)
    names += ["libmpv.so.2", "libmpv.so.1", "libmpv.so"]
    return names


def library():
    """Load libmpv once; raise OSError with a helpful message if it is missing."""
    global _lib, _load_error
    if _lib is not None:
        return _lib
    errors = []
    for name in _candidates():
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    else:
        _load_error = "; ".join(errors)
        raise OSError("libmpv was not found. Install mpv (Arch: mpv, Debian/Ubuntu: libmpv2, "
                      "Fedora: mpv-libs, openSUSE: libmpv2). Details: " + _load_error)

    lib.mpv_client_api_version.restype = c_ulong
    lib.mpv_create.restype = c_void_p
    lib.mpv_initialize.argtypes = [c_void_p]
    lib.mpv_terminate_destroy.argtypes = [c_void_p]
    lib.mpv_error_string.restype = c_char_p
    lib.mpv_error_string.argtypes = [c_int]
    lib.mpv_set_option_string.argtypes = [c_void_p, c_char_p, c_char_p]
    lib.mpv_set_property_string.argtypes = [c_void_p, c_char_p, c_char_p]
    lib.mpv_set_property.argtypes = [c_void_p, c_char_p, c_int, c_void_p]
    lib.mpv_get_property.argtypes = [c_void_p, c_char_p, c_int, c_void_p]
    lib.mpv_get_property_string.restype = c_void_p
    lib.mpv_get_property_string.argtypes = [c_void_p, c_char_p]
    lib.mpv_free.argtypes = [c_void_p]
    lib.mpv_command.argtypes = [c_void_p, POINTER(c_char_p)]
    lib.mpv_command_async.argtypes = [c_void_p, c_uint64, POINTER(c_char_p)]
    lib.mpv_observe_property.argtypes = [c_void_p, c_uint64, c_char_p, c_int]
    lib.mpv_request_log_messages.argtypes = [c_void_p, c_char_p]
    lib.mpv_wait_event.restype = POINTER(Event)
    lib.mpv_wait_event.argtypes = [c_void_p, c_double]
    lib.mpv_set_wakeup_callback.argtypes = [c_void_p, c_void_p, c_void_p]
    if hasattr(lib, "mpv_hook_add"):  # client API 1.101 (mpv 0.30)
        lib.mpv_hook_add.argtypes = [c_void_p, c_uint64, c_char_p, c_int]
        lib.mpv_hook_continue.argtypes = [c_void_p, c_uint64]
    lib.mpv_render_context_create.argtypes = [POINTER(c_void_p), c_void_p, POINTER(RenderParam)]
    lib.mpv_render_context_set_update_callback.argtypes = [c_void_p, c_void_p, c_void_p]
    lib.mpv_render_context_update.restype = c_uint64
    lib.mpv_render_context_update.argtypes = [c_void_p]
    lib.mpv_render_context_render.argtypes = [c_void_p, POINTER(RenderParam)]
    lib.mpv_render_context_report_swap.argtypes = [c_void_p]
    lib.mpv_render_context_free.argtypes = [c_void_p]
    _lib = lib
    return lib


def available() -> bool:
    try:
        library()
        return True
    except OSError:
        return False


def load_error() -> str:
    try:
        library()
        return ""
    except OSError as exc:
        return str(exc)


def api_version() -> tuple[int, int]:
    v = library().mpv_client_api_version()
    return (v >> 16) & 0xFFFF, v & 0xFFFF


class MpvError(RuntimeError):
    pass


def _check(code: int, what: str) -> int:
    if code < 0:
        msg = library().mpv_error_string(code)
        raise MpvError(f"{what}: {msg.decode() if msg else code}")
    return code


def _b(value) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bool):
        return b"yes" if value else b"no"
    return str(value).encode("utf-8")


class Mpv:
    """One mpv player instance (client handle)."""

    def __init__(self, options: dict[str, object] | None = None):
        lib = library()
        # libmpv requires the C numeric locale.
        import locale
        try:
            locale.setlocale(locale.LC_NUMERIC, "C")
        except locale.Error:
            pass
        self._lib = lib
        self.handle = lib.mpv_create()
        if not self.handle:
            raise MpvError("mpv_create() failed")
        for key, value in (options or {}).items():
            code = lib.mpv_set_option_string(self.handle, _b(key), _b(value))
            if code < 0:
                # Unknown options differ between mpv versions; they must not break playback.
                pass
        _check(lib.mpv_initialize(self.handle), "mpv_initialize")
        self._wakeup_cb = None

    # -- lifecycle
    def terminate(self) -> None:
        if self.handle:
            handle, self.handle = self.handle, None
            self._lib.mpv_terminate_destroy(handle)

    @property
    def alive(self) -> bool:
        return bool(self.handle)

    # -- commands and properties
    def command(self, *args) -> None:
        if not self.handle:
            return
        arr = (c_char_p * (len(args) + 1))(*[_b(a) for a in args], None)
        _check(self._lib.mpv_command(self.handle, arr), f"command {args[0]}")

    def command_async(self, *args) -> None:
        if not self.handle:
            return
        arr = (c_char_p * (len(args) + 1))(*[_b(a) for a in args], None)
        _check(self._lib.mpv_command_async(self.handle, 0, arr), f"command {args[0]}")

    def set(self, name: str, value) -> bool:
        """Set a property; returns False (instead of raising) if mpv rejects it."""
        if not self.handle:
            return False
        if isinstance(value, bool):
            data = c_int(1 if value else 0)
            code = self._lib.mpv_set_property(self.handle, _b(name), FORMAT_FLAG, ctypes.byref(data))
        elif isinstance(value, int):
            data = c_int64(value)
            code = self._lib.mpv_set_property(self.handle, _b(name), FORMAT_INT64, ctypes.byref(data))
        elif isinstance(value, float):
            data = c_double(value)
            code = self._lib.mpv_set_property(self.handle, _b(name), FORMAT_DOUBLE, ctypes.byref(data))
        else:
            code = self._lib.mpv_set_property_string(self.handle, _b(name), _b(value))
        return code >= 0

    def get_string(self, name: str) -> str | None:
        if not self.handle:
            return None
        ptr = self._lib.mpv_get_property_string(self.handle, _b(name))
        if not ptr:
            return None
        try:
            return ctypes.string_at(ptr).decode("utf-8", "replace")
        finally:
            self._lib.mpv_free(ptr)

    def get_double(self, name: str) -> float | None:
        if not self.handle:
            return None
        data = c_double()
        if self._lib.mpv_get_property(self.handle, _b(name), FORMAT_DOUBLE, ctypes.byref(data)) < 0:
            return None
        return data.value

    def get_int(self, name: str) -> int | None:
        if not self.handle:
            return None
        data = c_int64()
        if self._lib.mpv_get_property(self.handle, _b(name), FORMAT_INT64, ctypes.byref(data)) < 0:
            return None
        return data.value

    def get_flag(self, name: str) -> bool | None:
        if not self.handle:
            return None
        data = c_int()
        if self._lib.mpv_get_property(self.handle, _b(name), FORMAT_FLAG, ctypes.byref(data)) < 0:
            return None
        return bool(data.value)

    def observe(self, name: str, fmt: int = FORMAT_STRING, userdata: int = 0) -> None:
        if self.handle:
            self._lib.mpv_observe_property(self.handle, userdata, _b(name), fmt)

    def request_log_messages(self, level: str = "warn") -> None:
        if self.handle:
            self._lib.mpv_request_log_messages(self.handle, _b(level))

    def hook_add(self, name: str, priority: int = 0) -> bool:
        """Pause the player at hook ``name`` until ``hook_continue`` (see EVENT_HOOK)."""
        if not self.handle or not hasattr(self._lib, "mpv_hook_add"):
            return False
        return self._lib.mpv_hook_add(self.handle, 0, _b(name), priority) >= 0

    def hook_continue(self, hook_id: int) -> None:
        if self.handle:
            self._lib.mpv_hook_continue(self.handle, hook_id)

    # -- events
    def set_wakeup_callback(self, fn) -> None:
        """``fn()`` is called from an mpv thread whenever events are pending."""
        self._wakeup_cb = CallbackFn(lambda _ctx: fn())
        self._lib.mpv_set_wakeup_callback(self.handle, ctypes.cast(self._wakeup_cb, c_void_p), None)

    def events(self):
        """Drain pending events without blocking; yields (event_id, error, payload)."""
        while self.handle:
            ev = self._lib.mpv_wait_event(self.handle, 0.0).contents
            if ev.event_id == EVENT_NONE:
                return
            payload = None
            if ev.event_id == EVENT_PROPERTY_CHANGE and ev.data:
                prop = ctypes.cast(ev.data, POINTER(EventProperty)).contents
                payload = (prop.name.decode(), _property_value(prop))
            elif ev.event_id == EVENT_END_FILE and ev.data:
                end = ctypes.cast(ev.data, POINTER(EventEndFile)).contents
                payload = (end.reason, end.error)
            elif ev.event_id == EVENT_HOOK and ev.data:
                hook = ctypes.cast(ev.data, POINTER(EventHook)).contents
                payload = ((hook.name or b"").decode(), hook.id)
            elif ev.event_id == EVENT_LOG_MESSAGE and ev.data:
                msg = ctypes.cast(ev.data, POINTER(EventLogMessage)).contents
                payload = ((msg.prefix or b"").decode(errors="replace"),
                           (msg.level or b"").decode(errors="replace"),
                           (msg.text or b"").decode(errors="replace").rstrip())
            yield ev.event_id, ev.error, payload
            if ev.event_id == EVENT_SHUTDOWN:
                return

    def error_string(self, code: int) -> str:
        msg = self._lib.mpv_error_string(code)
        return msg.decode() if msg else str(code)


def _property_value(prop: EventProperty):
    if not prop.data:
        return None
    if prop.format == FORMAT_STRING:
        ptr = ctypes.cast(prop.data, POINTER(c_char_p)).contents
        return ptr.value.decode("utf-8", "replace") if ptr.value is not None else None
    if prop.format == FORMAT_FLAG:
        return bool(ctypes.cast(prop.data, POINTER(c_int)).contents.value)
    if prop.format == FORMAT_INT64:
        return ctypes.cast(prop.data, POINTER(c_int64)).contents.value
    if prop.format == FORMAT_DOUBLE:
        return ctypes.cast(prop.data, POINTER(c_double)).contents.value
    return None


class RenderContext:
    """mpv's OpenGL render context. Create, render and free with the GL context current."""

    def __init__(self, player: Mpv, get_proc_address):
        lib = library()
        self._lib = lib
        self._gpa = GetProcAddressFn(lambda _ctx, name: get_proc_address(name) or 0)
        init = OpenGLInitParams(self._gpa, None, None)
        api_type = ctypes.create_string_buffer(b"opengl")
        params = (RenderParam * 3)(
            RenderParam(RENDER_PARAM_API_TYPE, ctypes.cast(api_type, c_void_p)),
            RenderParam(RENDER_PARAM_OPENGL_INIT_PARAMS, ctypes.cast(ctypes.pointer(init), c_void_p)),
            RenderParam(RENDER_PARAM_INVALID, None),
        )
        self.ctx = c_void_p()
        _check(lib.mpv_render_context_create(ctypes.byref(self.ctx), player.handle, params),
               "mpv_render_context_create")
        self._update_cb = None

    def set_update_callback(self, fn) -> None:
        self._update_cb = CallbackFn(lambda _ctx: fn())
        self._lib.mpv_render_context_set_update_callback(self.ctx, ctypes.cast(self._update_cb, c_void_p), None)

    def update(self) -> int:
        return self._lib.mpv_render_context_update(self.ctx) if self.ctx else 0

    def render(self, fbo: int, width: int, height: int, flip_y: bool = True) -> None:
        if not self.ctx:
            return
        fbo_s = OpenGLFbo(int(fbo), int(width), int(height), 0)
        flip = c_int(1 if flip_y else 0)
        block = c_int(0)
        params = (RenderParam * 4)(
            RenderParam(RENDER_PARAM_OPENGL_FBO, ctypes.cast(ctypes.pointer(fbo_s), c_void_p)),
            RenderParam(RENDER_PARAM_FLIP_Y, ctypes.cast(ctypes.pointer(flip), c_void_p)),
            RenderParam(RENDER_PARAM_BLOCK_FOR_TARGET_TIME, ctypes.cast(ctypes.pointer(block), c_void_p)),
            RenderParam(RENDER_PARAM_INVALID, None),
        )
        self._lib.mpv_render_context_render(self.ctx, params)

    def report_swap(self) -> None:
        if self.ctx:
            self._lib.mpv_render_context_report_swap(self.ctx)

    def free(self) -> None:
        if self.ctx:
            ctx, self.ctx = self.ctx, c_void_p()
            # Detach the callback first so a late notification cannot race the free.
            self._lib.mpv_render_context_set_update_callback(ctx, None, None)
            self._lib.mpv_render_context_free(ctx)
