from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timedelta

import codexctl.sites.inventory as sites
import codexctl.sites.registry as site_registry
from codexctl.web.charts import comparison_series


def site_comparison(conn: sqlite3.Connection, days: int, now: int) -> dict:
    """Group the latest server inventories by site creation date in Beijing time."""
    today = datetime.fromtimestamp(now, sites.BEIJING).date()
    start = today - timedelta(days=days - 1)
    servers = site_registry.list_servers(conn)
    inventories = sites.snapshots(conn)
    ranking, pending, stale = [], [], []
    unknown_dates = 0
    for server in servers:
        inventory = inventories.get(server.id)
        if not inventory or inventory.revision != server.revision or inventory.synced_at is None:
            pending.append(server.name)
            continue
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
        daily.append({
            "day": key, "label": day.strftime("%m-%d"), "values": values, "total": sum(values),
            "show_label": days <= 7 or i in (0, days - 1) or i % (5 if days <= 30 else 15) == 0,
        })
    maximum = max((value for day in daily for value in day["values"]), default=0)
    # An even integer scale keeps low site counts readable, including an all-zero range.
    scale = max(2, maximum + maximum % 2)
    series = comparison_series(daily, [item["name"] for item in ranking], scale)
    return {
        "ranking": ranking, "daily": daily, "series": series, "scale": scale,
        "total": sum(item["total"] for item in ranking), "today": daily[-1]["total"],
        "server_count": len(servers), "pending": pending, "stale": stale, "unknown_dates": unknown_dates,
    }
