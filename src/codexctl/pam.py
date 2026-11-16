from __future__ import annotations

import ctypes as C
import logging
from functools import lru_cache
from pathlib import Path

_SERVICE = b"codexctl"
_CONFIG_DIR = Path("/etc/pam.d")
_SUCCESS = 0
_BUF_ERR = 5
_CONV_ERR = 19
_DISALLOW_NULL_AUTHTOK = 1
_PROMPT_ECHO_OFF = 1
_ERROR_MSG = 3
_TEXT_INFO = 4
_MAX_NUM_MSG = 32
log = logging.getLogger(__name__)


class _Message(C.Structure):
    _fields_ = [("style", C.c_int), ("text", C.c_char_p)]


class _Response(C.Structure):
    _fields_ = [("text", C.c_void_p), ("code", C.c_int)]


_ConversationFn = C.CFUNCTYPE(
    C.c_int, C.c_int, C.POINTER(C.POINTER(_Message)), C.POINTER(C.POINTER(_Response)), C.c_void_p,
)


class _Conversation(C.Structure):
    _fields_ = [("callback", _ConversationFn), ("data", C.c_void_p)]


@lru_cache(maxsize=1)
def _libraries() -> tuple[C.CDLL, C.CDLL]:
    pam = C.CDLL("libpam.so.0")
    pam.pam_start_confdir.argtypes = [
        C.c_char_p, C.c_char_p, C.POINTER(_Conversation), C.c_char_p, C.POINTER(C.c_void_p),
    ]
    for fn in (pam.pam_authenticate, pam.pam_acct_mgmt, pam.pam_end):
        fn.argtypes = [C.c_void_p, C.c_int]

    libc = C.CDLL(None)
    libc.calloc.argtypes = [C.c_size_t, C.c_size_t]
    libc.calloc.restype = C.c_void_p
    libc.strdup.argtypes = [C.c_char_p]
    libc.strdup.restype = C.c_void_p
    libc.free.argtypes = [C.c_void_p]
    libc.free.restype = None
    return pam, libc


def verify_password(username: str, password: str) -> bool:
    """Authenticate against the dedicated local Linux PAM service."""
    if not password or "\0" in password:
        return False
    # Linux-PAM falls back to the 'other' stack for missing service files.
    if not (_CONFIG_DIR / _SERVICE.decode()).is_file():
        log.error("codexctl PAM service is missing")
        return False
    try:
        pam, libc = _libraries()
    except (OSError, AttributeError):
        log.exception("Linux-PAM is unavailable")
        return False
    secret = password.encode()

    @_ConversationFn
    def respond(count, messages, output, _data):
        if not 0 < count <= _MAX_NUM_MSG:
            return _CONV_ERR
        styles = [messages[i].contents.style for i in range(count)]
        if any(style not in (_PROMPT_ECHO_OFF, _ERROR_MSG, _TEXT_INFO) for style in styles):
            return _CONV_ERR
        address = libc.calloc(count, C.sizeof(_Response))
        if not address:
            return _BUF_ERR
        responses = C.cast(address, C.POINTER(_Response))
        for i, style in enumerate(styles):
            if style == _PROMPT_ECHO_OFF:
                responses[i].text = libc.strdup(secret)
                if not responses[i].text:
                    for j in range(i):
                        libc.free(responses[j].text)
                    libc.free(address)
                    return _BUF_ERR
        output[0] = responses  # PAM owns the response array and strings after success.
        return _SUCCESS

    handle = C.c_void_p()
    conversation = _Conversation(respond, None)
    status = pam.pam_start_confdir(_SERVICE, username.encode(), C.byref(conversation), bytes(_CONFIG_DIR), C.byref(handle))
    if status != _SUCCESS:
        log.error("could not start codexctl PAM service (status %s)", status)
        return False
    try:
        status = pam.pam_authenticate(handle, _DISALLOW_NULL_AUTHTOK)
        if status == _SUCCESS:
            status = pam.pam_acct_mgmt(handle, _DISALLOW_NULL_AUTHTOK)
        return status == _SUCCESS
    finally:
        pam.pam_end(handle, status)
