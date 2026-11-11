from __future__ import annotations

import grp
import os
import socket
from pathlib import Path

import uvicorn

from ..context import Context
from .app import create_app


def serve(ctx: Context) -> None:
    s = ctx.settings
    app = create_app(ctx)
    if not s.web_socket:
        uvicorn.run(app, host=s.web_host, port=s.web_port, proxy_headers=False, access_log=True, log_level="info")
        return
    path = Path(s.web_socket)
    gid = grp.getgrnam(s.web_socket_group).gr_gid
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(path.parent, -1, gid)
    os.chmod(path.parent, 0o750)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    os.chown(path, -1, gid)
    os.chmod(path, 0o660)
    try:
        uvicorn.run(app, fd=sock.fileno(), proxy_headers=False, access_log=True, log_level="info")
    finally:
        sock.close()
