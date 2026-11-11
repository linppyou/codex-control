from __future__ import annotations

from ..cli import fmt_ts


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
