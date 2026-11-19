from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import codexctl.sites.inventory as sites
import codexctl.usage.pricing as pricing
import codexctl.usage.repository as usage_repo
from codexctl.formatting import clean_text


def usage_windows(ctx) -> tuple[int, int]:
    """Return Beijing midnight and the start of the last seven calendar days."""
    midnight = datetime.fromtimestamp(ctx.now(), sites.BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp()), int((midnight - timedelta(days=6)).timestamp())


def usage_user_rows(ctx, lock) -> tuple[list[sqlite3.Row], pricing.PriceBook]:
    today, week = usage_windows(ctx)
    with lock:
        rows = usage_repo.usage_by_user_model_windows(ctx.conn, today, week)
        return rows, pricing.PriceBook(usage_repo.list_prices(ctx.conn))


def group_users(rows: list[sqlite3.Row], labels: dict[str, list[str]], book: pricing.PriceBook) -> list[dict]:
    """把 (用户, 模型) 行聚合成每个用户一项，内含按近 7 天降序的模型明细与各时段估算费用。"""
    users: dict[str, dict] = {}
    for r in rows:
        u = users.setdefault(r["username"], {
            "username": clean_text(r["username"]), "raw": r["username"],
            "labels": [clean_text(x) for x in labels.get(r["username"], [])],
            "today": 0, "week": 0, "total": 0, "turns": 0, "models": [],
            "cost_today": None, "cost_week": None, "cost_total": None,
            "unpriced_today": 0, "unpriced_week": 0, "unpriced_total": 0,
        })
        m = {
            "name": clean_text(r["model"]) if r["model"] else "未知模型", "known": bool(r["model"]),
            "today": r["today"] or 0, "week": r["week"] or 0, "total": r["total"] or 0, "turns": r["turns"],
        }
        for k in ("today", "week", "total"):
            cost = book.cost(r["model"], r[f"{k}_input"], r[f"{k}_cached"], r[f"{k}_output"])
            m[f"cost_{k}"] = cost
            if m[k]:
                if cost is None:
                    u[f"unpriced_{k}"] += 1
                else:
                    u[f"cost_{k}"] = (u[f"cost_{k}"] or 0) + cost
        u["models"].append(m)
        for k in ("today", "week", "total", "turns"):
            u[k] += m[k]
    return sorted(users.values(), key=lambda u: (-u["week"], -u["total"], u["username"]))


def user_comparison(ctx, days: int, *, now: int | None = None) -> dict:
    """Build the per-user dashboard comparison; caller holds the lock."""
    days = days if days in (7, 30, 90) else 7
    local_today = datetime.fromtimestamp(ctx.now() if now is None else now, sites.BEIJING).date()
    start_day = local_today - timedelta(days=days - 1)
    start_ts = int(datetime.combine(start_day, datetime.min.time(), sites.BEIJING).timestamp())
    end_ts = int(datetime.combine(local_today + timedelta(days=1), datetime.min.time(), sites.BEIJING).timestamp())
    book = pricing.PriceBook(usage_repo.list_prices(ctx.conn))
    daily_rows = usage_repo.usage_daily_by_user_model(ctx.conn, start_ts, end_ts)
    comparison = {}
    daily_totals = {}
    for r in daily_rows:
        u = comparison.setdefault(r["username"], {"name": clean_text(r["username"]), "raw": r["username"],
            "total": 0, "input": 0, "cached": 0, "output": 0, "turns": 0, "cost": 0.0,
            "priced": False, "unpriced": False})
        for key in ("total", "input", "cached", "output", "turns"):
            u[key] += r[key] or 0
        cost = book.cost(r["model"], r["input"], r["cached"], r["output"])
        if cost is None and (r["total"] or 0):
            u["unpriced"] = True
        elif cost is not None:
            u["cost"] += cost
            u["priced"] = True
        key = (r["day"], r["username"])
        daily_totals[key] = daily_totals.get(key, 0) + (r["total"] or 0)
    ranking = sorted(comparison.values(), key=lambda u: (-u["total"], u["raw"]))
    daily = []
    for i in range(days):
        day = start_day + timedelta(days=i)
        key = day.isoformat()
        values = [daily_totals.get((key, u["raw"]), 0) for u in ranking]
        daily.append({"day": key, "values": values, "total": sum(values)})
    total_tokens = sum(u["total"] for u in ranking)
    total_turns = sum(u["turns"] for u in ranking)
    total_cost = sum(u["cost"] for u in ranking)
    has_price = any(u["priced"] for u in ranking)
    has_unpriced = any(u["unpriced"] for u in ranking)
    total_input = sum(u["input"] for u in ranking)
    total_cached = sum(u["cached"] for u in ranking)
    return dict(days=days, ranking=ranking, daily=daily,
                total_tokens=total_tokens, total_turns=total_turns, total_cost=total_cost,
                has_price=has_price, has_unpriced=has_unpriced, total_input=total_input, total_cached=total_cached)


def comparison_series(daily: list[dict], names: list[str], maximum: int, *, width: int = 960) -> list[dict]:
    """Add coordinates to daily rows and build matching comparison lines."""
    series = [{"name": name, "index": i, "points": []} for i, name in enumerate(names)]
    for i, day in enumerate(daily):
        day["x"] = round(70 + i * (width - 110) / max(1, len(daily) - 1), 2)
        for index, item in enumerate(series):
            value = day["values"][index]
            y = round(175 - max(0, value) / (maximum or 1) * 140, 2)
            item["points"].append({"x": day["x"], "y": y, "value": value, "day": day["day"]})
    for item in series:
        item["path"] = " ".join(
            ("M" if i == 0 else "L") + f"{point['x']},{point['y']}"
            for i, point in enumerate(item["points"])
        )
    return series


def dashboard_comparison_chart(daily: list[dict], names: list[str]) -> dict:
    """Keep detail rows daily and group the longest chart by Beijing calendar week."""
    days = len(daily)
    weekly = days == 90
    periods = []
    for row in daily:
        day = date.fromisoformat(row["day"])
        week = day - timedelta(days=day.weekday()) if weekly else day
        if not periods or periods[-1]["week"] != week:
            periods.append({"week": week, "start": day, "end": day, "values": list(row["values"]), "total": row["total"]})
        else:
            period = periods[-1]
            period["end"] = day
            period["values"] = [left + right for left, right in zip(period["values"], row["values"])]
            period["total"] += row["total"]

    last = len(periods) - 1
    label_count = min(len(periods), 7 if days == 7 else 5)
    labels = {round(last * i / max(1, label_count - 1)) for i in range(label_count)}
    mobile_labels = {0, round(last / 2), last}
    for i, period in enumerate(periods):
        start, end = period["start"], period["end"]
        period["day"] = f"{start} 至 {end}" if weekly and start != end else str(start)
        period["label"] = start.strftime("%m-%d")
        period["show_label"] = i in labels or i in mobile_labels
        period["mobile_label"] = i in mobile_labels
        period["partial"] = weekly and (end - start).days < 6

    maximum = max((value for period in periods for value in period["values"]), default=0)
    step = max(1, (maximum + 3) // 4)
    scale = max(2, ((maximum + step - 1) // step) * step)
    series = comparison_series(periods, names, scale, width=600)
    return {
        "periods": periods, "series": series, "weekly": weekly,
        "default_visible": min(3, len(names)),
        "show_points": days != 30,
        "step_x": 490 / max(1, last),
        "y_ticks": [(175 - value / scale * 140, value) for value in range(0, scale + 1, step)],
    }

