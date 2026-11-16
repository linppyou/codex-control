#!/usr/bin/python3
"""Emit the site inventory for a restricted SSH key."""

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

PANEL = Path("/www/server/panel")
SERVER_NAME = re.compile(r"^\s*server_name\s+([^;]+);", re.MULTILINE)


def timestamp(value):
    try:
        return int(datetime.fromisoformat(value).astimezone(timezone.utc).timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def collect(panel):
    database = panel / "data/db/site.db"
    if not database.is_file():
        database = panel / "data/default.db"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
        conn.execute("BEGIN")
        bindings = {}
        for site_id, domain in conn.execute("SELECT pid, name FROM domain ORDER BY id"):
            bindings.setdefault(site_id, []).append(domain)
        sites = [
            {"id": site_id, "name": name, "created_at": timestamp(addtime), "domains": bindings.get(site_id, [])}
            for site_id, name, addtime in conn.execute("SELECT id, name, addtime FROM sites ORDER BY id")
        ]

    vhosts = {}
    directory = panel / "vhost/nginx"
    for path in sorted(directory.glob("*.conf")):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue  # The panel can remove the config after globbing.
        content = re.sub(r"#[^\n]*", "", content)
        names = sorted({name for match in SERVER_NAME.findall(content) for name in match.split()})
        if names:
            vhosts[path.stem] = names

    return {"version": 1, "sites": sites, "vhosts": vhosts}


if __name__ == "__main__":
    if os.environ.get("SSH_ORIGINAL_COMMAND", "codexctl-site-inventory") != "codexctl-site-inventory":
        raise SystemExit("Unsupported command")
    print(json.dumps(collect(PANEL), ensure_ascii=True))
