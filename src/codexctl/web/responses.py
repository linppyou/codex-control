from __future__ import annotations

from urllib.parse import urlencode

from fastapi.responses import RedirectResponse


def _redirect(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    params = {}
    if msg:
        params["msg"] = msg[:500]
    if err:
        params["err"] = err[:500]
    separator = "&" if "?" in path else "?"
    return RedirectResponse(path + (separator + urlencode(params) if params else ""), status_code=303)


def _flash(msg: str, err: str) -> dict:
    out = {}
    if msg:
        out["msg"] = msg
    if err:
        out["err"] = err
    return out


def _csv_cell(v: object) -> object:
    if isinstance(v, str) and v and v[0] in "=+-@\t\r":
        return "'" + v
    return v

