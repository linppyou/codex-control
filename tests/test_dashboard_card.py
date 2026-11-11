import time
from types import SimpleNamespace

from codexctl.web.charts import week_trend_chart
from codexctl.web.views import _card, _mask_email

NOW = 1_800_000_000
ST = SimpleNamespace(quota_poll_seconds=600)
ACC = SimpleNamespace(label="BWG", email="alice.long@example.com", plan_type="pro", status="active")


def row(used, resets, minutes=10080, win="secondary", ts=NOW, source="api"):
    return {"win": win, "used_percent": used, "window_minutes": minutes, "resets_at": resets, "ts": ts, "source": source}


def md_hm(ts):
    t = time.localtime(ts)
    return f"{t.tm_mon}/{t.tm_mday} {t.tm_hour:02d}:{t.tm_min:02d}"


def test_mask_email():
    assert _mask_email("alice.long@example.com") == "alic****@example.com"
    assert _mask_email("ab@x.io") == "ab****@x.io"
    assert _mask_email(None) == "-"


def test_card_basic_and_tones():
    resets = NOW + 3 * 86400
    c = _card(ACC, [], [row(37.4, resets)], NOW, ST)
    assert c["email"] == "alic****@example.com" and c["plan"] == "PRO" and c["status_text"] == ""
    assert c["remaining"] == 63 and c["used"] == 37 and c["tone"] == "good"
    assert c["reset"] == md_hm(resets) and c["synced"] == md_hm(NOW)
    assert _card(ACC, [], [row(80, resets)], NOW, ST)["tone"] == "warn"
    assert _card(ACC, [], [row(95, resets)], NOW, ST)["tone"] == "bad"
    assert _card(ACC, [], [row(70, resets)], NOW, ST)["tone"] == "good"


def test_card_five_hour_status_and_no_data():
    resets = NOW + 86400
    five = row(20, NOW + 3600, 300, "primary")
    c = _card(SimpleNamespace(**{**ACC.__dict__, "status": "needs_relogin"}), [], [five, row(10, resets)], NOW, ST)
    assert c["other"]["name"] == "5小时" and c["other"]["used"] == 20 and c["status_text"] == "需重新登录"
    assert c["remaining"] == 90
    n = _card(ACC, [], [], NOW, ST)
    assert not n["has_quota"] and n["synced"] == "-"


def test_projection_cases():
    resets = NOW + 4 * 86400  # window_start = NOW - 3d, elapsed 3d
    c = _card(ACC, [], [row(50, resets)], NOW, ST)  # exhaust at start + 6d > ... = NOW+3d < resets
    assert c["projection"] == f"按此速度，预计 {md_hm(NOW + 3 * 86400)} 用完"
    assert _card(ACC, [], [row(30, resets)], NOW, ST)["projection"] == "按此速度，重置前不会用完"
    assert _card(ACC, [], [row(0, resets)], NOW, ST)["projection"] == "本周尚未使用"


def test_card_history_filter():
    resets = NOW + 86400
    hist = [
        {"ts": NOW, "primary": None, "secondary": {"used_percent": 40, "window_minutes": 10080, "resets_at": resets}},
        {"ts": NOW - 600, "primary": None, "secondary": {"used_percent": 99, "window_minutes": 10080, "resets_at": resets - 7 * 86400}},
    ]
    c = _card(ACC, hist, [row(40, resets)], NOW, ST)
    assert c["chart"]["path"].count("M") == 1 and c["chart"]["path"].count("L") == 0


def test_week_chart_geometry():
    s, e = 1000, 11000
    ch = week_trend_chart([(1000, 100.0), (6000, 50.0)], window_start=s, window_end=e, now=6000, gap=9999)
    assert ch["pace"] == (36, 8, 422, 112)
    assert ch["now_x"] == 229.0
    assert ch["path"] == "M 36.0 8.0 L 229.0 60.0"
    assert [t[1] for t in ch["y_ticks"]] == ["100%", "50%", "0%"] and [t[0] for t in ch["y_ticks"]] == [8, 60, 112]
    assert len(ch["x_ticks"]) == 2 and ch["last"][2].endswith("剩余 50%")


def test_week_chart_gap_outside_empty():
    s, e = 1000, 11000
    ch = week_trend_chart([(2000, 90.0), (2600, 80.0), (5000, 70.0), (500, 10.0), (12000, 5.0)],
                          window_start=s, window_end=e, now=3000, gap=1000)
    assert ch["path"].count("M") == 2 and ch["path"].count("L") == 1
    empty = week_trend_chart([], window_start=s, window_end=e, now=3000, gap=1000)
    assert empty["path"] == "" and empty["last"] is None and empty["now_x"] is not None and empty["pace"]
