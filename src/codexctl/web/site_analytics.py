from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timedelta

import codexctl.sites.inventory as sites
import codexctl.sites.registry as site_registry


def site_comparison(conn: sqlite3.Connection, days: int, now: int) -> dict:
    """Group the latest server inventories by site creation date in Beijing time."""
    today = datetime.fromtimestamp(now, sites.BEIJING).date()
    start = today - timedelta(days=days - 1)
    servers = site_registry.list_servers(conn)
    inventories = sites.snapshots(conn)
    ranking, pending, stale = [], [], []
    site_counts = {}
    unknown_dates = 0
    for server in servers:
        inventory = inventories.get(server.id)
        if not inventory or inventory.revision != server.revision or inventory.synced_at is None:
            pending.append(server.name)
            continue
        site_counts[server.id] = len(inventory.rows)
        counts = Counter(sites.deployment_date(row["created_at"]) for row in inventory.rows)
        unknown_dates += counts[None]
        total = sum(count for day, count in counts.items() if day and start.isoformat() <= day <= today.isoformat())
        ranking.append({"name": server.name, "id": server.id, "total": total, "counts": counts})
        if inventory.error or now - inventory.synced_at > sites.STALE_AFTER:
            stale.append(server.name)
    ranking.sort(key=lambda item: (-item["total"], item["name"], item["id"]))
    daily = []
    for i in range(days):
        day = start + timedelta(days=i)
        key = day.isoformat()
        values = [item["counts"][key] for item in ranking]
        daily.append({"day": key, "values": values, "total": sum(values)})
    return {
        "ranking": ranking, "daily": daily,
        "total": sum(item["total"] for item in ranking), "today": daily[-1]["total"],
        "site_counts": site_counts,
        "server_count": len(servers), "pending": pending, "stale": stale, "unknown_dates": unknown_dates,
    }
