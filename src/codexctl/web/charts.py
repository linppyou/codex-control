from __future__ import annotations

import time

from ..cli import fmt_ts


def fmt_md(ts: int) -> str:
    t = time.localtime(ts)
    return f"{t.tm_mon}/{t.tm_mday}"


def fmt_md_hm(ts: int) -> str:
    t = time.localtime(ts)
    return f"{t.tm_mon}/{t.tm_mday} {t.tm_hour:02d}:{t.tm_min:02d}"


TOKEN_UNITS = ("", "K", "M", "B", "T")


def comparison_series(daily: list[dict], names: list[str], maximum: int) -> list[dict]:
    """Add coordinates to daily rows and build matching comparison lines."""
    series = [{"name": name, "index": i, "points": []} for i, name in enumerate(names)]
    for i, day in enumerate(daily):
        day["x"] = round(70 + i * 850 / max(1, len(daily) - 1), 2)
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


def fmt_tokens(n: int | float | None) -> str:
    """把 token 数量格式化为带单位的短字符串：999、1.23K、45.6M、789B，保留 3 位有效数字。"""
    v = float(n or 0)
    sign = "-" if v < 0 else ""
    v = abs(v)
    i = 0
    while i < len(TOKEN_UNITS) - 1 and round(v, 2 if v < 10 else 1 if v < 100 else 0) >= 1000:
        v /= 1000
        i += 1
    if i == 0:
        return f"{sign}{int(round(v))}"
    text = f"{v:.2f}" if v < 10 else f"{v:.1f}" if v < 100 else f"{v:.0f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{sign}{text}{TOKEN_UNITS[i]}"


def quota_chart(
    points: list[tuple[int, float]], *, width: int = 720, height: int = 240, pad: int = 40, gap: int | None = None
) -> dict | None:
    """把 (时间戳, 剩余百分比) 折线算成 SVG 几何；y 轴固定 0–100%，x 轴从首点到末点。

    gap 非空时，相邻点间隔超过 gap 秒就另起一段（M），避免把缺失数据画成直线。
    """
    pts = sorted(points)
    if not pts:
        return None
    inner_w, inner_h = width - 2 * pad, height - 2 * pad
    t0, t1 = pts[0][0], pts[-1][0]

    def px(ts: int) -> float:
        return pad + inner_w / 2 if t1 == t0 else pad + inner_w * (ts - t0) / (t1 - t0)

    def py(v: float) -> float:
        return pad + inner_h * (1 - max(0.0, min(100.0, v)) / 100)

    path, dots, prev = [], [], None
    for ts, v in pts:
        x, y = px(ts), py(v)
        cmd = "M" if prev is None or (gap is not None and ts - prev > gap) else "L"
        path.append(f"{cmd} {x:.1f} {y:.1f}")
        dots.append((round(x, 1), round(y, 1), f"{fmt_ts(ts)} 剩余 {max(0, min(100, round(v)))}%"))
        prev = ts
    n = 1 if t1 == t0 else min(6, len(pts))
    ticks = [t0] if n == 1 else [t0 + (t1 - t0) * i // (n - 1) for i in range(n)]
    return {
        "path": " ".join(path),
        "dots": dots,
        "y_ticks": [(round(py(v), 1), f"{v}%") for v in (0, 25, 50, 75, 100)],
        "x_ticks": [(round(px(t), 1), fmt_ts(t)) for t in ticks],
        "width": width, "height": height, "pad": pad,
    }


def week_trend_chart(
    samples: list[tuple[int, float]], *, window_start: int, window_end: int, now: int, gap: int,
    width: int = 430, height: int = 130, pad_left: int = 36, pad_bottom: int = 18, pad_top: int = 8, pad_right: int = 8,
) -> dict:
    """本周窗口走势：x 轴为整个 [window_start, window_end]，y 轴 0–100% 剩余额度。

    窗口外的样本忽略；相邻样本间隔超过 gap 秒就另起一段（M）。
    """
    left, right = pad_left, width - pad_right
    top, bottom = pad_top, height - pad_bottom
    span = max(1, window_end - window_start)

    def px(ts: float) -> float:
        return left + (right - left) * (ts - window_start) / span

    def py(v: float) -> float:
        return top + (bottom - top) * (1 - max(0.0, min(100.0, v)) / 100)

    pts = sorted((ts, v) for ts, v in samples if window_start <= ts <= window_end)
    path, prev = [], None
    for ts, v in pts:
        cmd = "M" if prev is None or ts - prev > gap else "L"
        path.append(f"{cmd} {px(ts):.1f} {py(v):.1f}")
        prev = ts
    last = None
    if pts:
        ts, v = pts[-1]
        last = (round(px(ts), 1), round(py(v), 1), f"{fmt_md_hm(ts)} 剩余 {max(0, min(100, round(v)))}%")
    return {
        "path": " ".join(path),
        "last": last,
        "pace": (round(px(window_start), 1), round(py(100), 1), round(px(window_end), 1), round(py(0), 1)),
        "now_x": round(px(now), 1) if window_start <= now <= window_end else None,
        "y_ticks": [(round(py(v), 1), f"{v}%") for v in (100, 50, 0)],
        "x_ticks": [(round(px(window_start), 1), fmt_md(window_start), "start"),
                    (round(px(window_end), 1), fmt_md(window_end), "end")],
        "box": (left, right, top, bottom),
        "width": width, "height": height,
    }
