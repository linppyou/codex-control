#!/usr/bin/python3
"""Emit the site inventory for a restricted SSH key."""

import json
import os
import re
import sqlite3
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

PANEL = Path("/www/server/panel")
SERVER_NAME = re.compile(r"^\s*server_name\s+([^;]+);", re.MULTILINE)
MYSQL_CLIENTS = ("/usr/bin/mysql", "/usr/bin/mariadb", "/www/server/mysql/bin/mysql")
PHP_STRING = r"""'(?:\\.|[^'\\\r\n])*'""" r'|"[^"\\$\r\n]*"'
PHP_COMMENTS = re.compile(
    r"""(?P<comment>/\*.*?\*/|//[^\r\n]*|\#[^\r\n]*)|'(?:\\.|[^'\\])*'""" r'|"(?:\\.|[^"\\])*"',
    re.DOTALL,
)
DB_SETTING = re.compile(
    rf"""^[ \t]*define\s*\(\s*['"](DB_NAME|DB_USER|DB_PASSWORD|DB_HOST)['"]\s*,\s*({PHP_STRING})\s*\)\s*;""",
    re.MULTILINE,
)
TABLE_PREFIX = re.compile(r"""^[ \t]*\$table_prefix\s*=\s*['"]([A-Za-z0-9_]+)['"]\s*;""", re.MULTILINE)
LOCAL_DB = re.compile(r"(localhost|127\.0\.0\.1)(?::([0-9]{1,5}))?")
DATA_BUDGET = 10
QUERY_TIMEOUT = 2


def timestamp(value):
    try:
        return int(datetime.fromisoformat(value).astimezone(timezone.utc).timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def wordpress_config(root):
    path = Path(root) / "wp-config.php"
    if not path.is_absolute():
        raise ValueError("expected absolute site path")
    with open(
        path, encoding="utf-8",
        opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK),
    ) as config:
        if not stat.S_ISREG(os.fstat(config.fileno()).st_mode):
            raise ValueError("expected a regular config file")
        content = config.read(65537)
    if len(content) > 65536:
        raise ValueError("config file too large")
    content = PHP_COMMENTS.sub(lambda match: "\n" * match[0].count("\n") if match["comment"] else match[0], content)
    settings = DB_SETTING.findall(content)
    prefixes = TABLE_PREFIX.findall(content)
    if len(settings) != 4 or len(prefixes) != 1:
        raise ValueError("expected literal WordPress settings")
    values = {key: re.sub(r"\\(['\\])", r"\1", value[1:-1]) for key, value in settings}
    return values, prefixes[0]


def data_query(prefix):
    posts = f"`{prefix}posts`"
    terms = f"`{prefix}terms`"
    taxonomy = f"`{prefix}term_taxonomy`"
    relationships = f"`{prefix}term_relationships`"
    # Match preset items in the shared templates. New items in those menus still count.
    return f"""
        SELECT
            (SELECT COUNT(*) FROM {posts} WHERE post_type = 'product' AND post_status = 'publish'),
            (SELECT COUNT(*) FROM {posts} WHERE post_type = 'product' AND post_status = 'importing'),
            (SELECT COUNT(DISTINCT taxonomy.term_taxonomy_id)
             FROM {taxonomy} AS taxonomy
             JOIN {terms} AS menu ON menu.term_id = taxonomy.term_id
             JOIN {relationships} AS rel USING (term_taxonomy_id)
             JOIN {posts} AS item ON item.ID = rel.object_id
             WHERE taxonomy.taxonomy = 'nav_menu'
               AND item.post_type = 'nav_menu_item' AND item.post_status = 'publish'
               AND NOT (
                   (menu.term_id = 77 AND menu.slug = 'mobile-navigation' AND item.ID = 538)
                   OR (menu.term_id = 472 AND menu.slug = 'f1' AND item.ID IN (13670, 13671, 15391, 15392))
                   OR (menu.term_id = 473 AND menu.slug = 'f2' AND item.ID IN (13672, 13673, 13674, 13675))
                   OR (menu.term_id = 24 AND menu.slug = 'menu' AND item.ID = 41328)
                   OR (menu.term_id = 25 AND menu.slug = 'foot' AND item.ID = 126)
               ))
            + (SELECT COUNT(*) FROM {posts} WHERE post_type = 'wp_navigation' AND post_status = 'publish'
               AND (post_content LIKE '%<!-- wp:navigation-link %'
                    OR post_content LIKE '%<!-- wp:navigation-submenu %'))
    """


def mysql_option(value):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("unsupported control character in database setting")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def collect_data(root, mysql, deadline):
    if not root or time.monotonic() >= deadline:
        return None
    try:
        settings, prefix = wordpress_config(root)
        address = LOCAL_DB.fullmatch(settings["DB_HOST"])
        if not address:
            return None
        host, configured_port = address.groups()
        port = int(configured_port or "3306")
        if not 1 <= port <= 65535:
            return None
        protocol = ["--protocol=TCP"] if configured_port is not None else []
        with NamedTemporaryFile(mode="w", encoding="utf-8", prefix="codexctl-site-db-") as defaults:
            defaults.write("[client]\n")
            for option in ("USER", "PASSWORD", "NAME"):
                name = "database" if option == "NAME" else option.lower()
                defaults.write(f"{name}={mysql_option(settings['DB_' + option])}\n")
            defaults.flush()
            result = subprocess.run(
                [
                    mysql, f"--defaults-file={defaults.name}", f"--host={host}", f"--port={port}", *protocol,
                    "--batch", "--skip-column-names", "--connect-timeout=1", "--skip-reconnect",
                    "--local-infile=0", "--execute", data_query(prefix),
                ],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=QUERY_TIMEOUT, check=True,
            )
        products, importing, menus = map(int, result.stdout.split())
        if min(products, importing, menus) >= 0:
            return {"products": products, "importing": importing, "menus": menus}
    except (OSError, KeyError, ValueError, subprocess.SubprocessError):
        # A failed WordPress check must not discard the server's inventory.
        pass
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
        columns = {column[1] for column in conn.execute("PRAGMA table_info(sites)")}
        path_column = "path" if "path" in columns else "NULL"
        status_column = "status" if "status" in columns else "NULL"
        remark_column = "ps" if "ps" in columns else "NULL"
        rows = conn.execute(
            f"SELECT id, name, addtime, {path_column}, {status_column}, {remark_column} FROM sites ORDER BY id"
        ).fetchall()
        sites = [
            {
                "id": site_id, "name": name, "created_at": timestamp(addtime),
                "domains": bindings.get(site_id, []), "path": path, "status": status, "remark": remark,
            }
            for site_id, name, addtime, path, status, remark in rows
        ]

    mysql = next((path for path in MYSQL_CLIENTS if os.access(path, os.X_OK)), None)
    if mysql and sites:
        deadline = time.monotonic() + DATA_BUDGET
        with ThreadPoolExecutor(max_workers=4) as workers:
            # Start with recent sites when a large inventory reaches the time limit.
            checks = [workers.submit(collect_data, row[3], mysql, deadline) for row in reversed(rows)]
            for site, check in zip(sites, reversed(checks)):
                site["data"] = check.result()

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
