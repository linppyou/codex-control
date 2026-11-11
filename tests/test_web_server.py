import dataclasses
import grp
import os
import stat

from codexctl.web import server


def test_serve_unix_socket(ctx, monkeypatch):
    import shutil
    import tempfile
    from pathlib import Path

    base = Path(tempfile.mkdtemp(prefix="cx"))  # AF_UNIX 路径长度有限
    sock_path = base / "run" / "web.sock"
    group = grp.getgrgid(os.getgid()).gr_name
    ctx.settings = dataclasses.replace(ctx.settings, web_socket=str(sock_path), web_socket_group=group)
    seen = {}

    def fake_run(app, **kw):
        seen.update(kw)
        seen["mode"] = stat.S_IMODE(sock_path.stat().st_mode)
        seen["dirmode"] = stat.S_IMODE(sock_path.parent.stat().st_mode)

    monkeypatch.setattr(server.uvicorn, "run", fake_run)
    sock_path.parent.mkdir()
    sock_path.write_text("stale")
    try:
        server.serve(ctx)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    assert seen["mode"] == 0o660 and seen["dirmode"] == 0o750
    assert "fd" in seen and "host" not in seen and seen["proxy_headers"] is False


def test_serve_tcp_when_socket_empty(ctx, monkeypatch):
    ctx.settings = dataclasses.replace(ctx.settings, web_socket="", web_host="127.0.0.1", web_port=8701)
    seen = {}
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kw: seen.update(kw))
    server.serve(ctx)
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8701 and seen["proxy_headers"] is False
