from codexctl.web.charts import quota_chart


def test_empty_and_single():
    assert quota_chart([]) is None
    c = quota_chart([(1_800_000_000, 50.0)])
    assert len(c["dots"]) == 1 and c["path"].startswith("M ") and len(c["x_ticks"]) == 1


def test_scaling_bounds_and_ticks():
    c = quota_chart([(0, 100.0), (600, 0.0)], width=720, height=240, pad=40)
    assert c["dots"][0][:2] == (40.0, 40.0) and c["dots"][1][:2] == (680.0, 200.0)
    assert c["path"] == "M 40.0 40.0 L 680.0 200.0"
    assert [t[1] for t in c["y_ticks"]] == ["0%", "25%", "50%", "75%", "100%"]
    assert c["y_ticks"][0][0] == 200.0 and c["y_ticks"][-1][0] == 40.0
    assert len(c["x_ticks"]) == 2
    many = quota_chart([(i * 600, 50.0) for i in range(50)])
    assert len(many["x_ticks"]) == 6


def test_gap_splits_segments():
    c = quota_chart([(0, 90.0), (600, 80.0), (5000, 70.0), (5600, 60.0)], gap=1800)
    assert c["path"].count("M") == 2 and c["path"].count("L") == 2
    assert quota_chart([(0, 90.0), (5000, 70.0)])["path"].count("M") == 1
    assert "剩余 90%" in c["dots"][0][2]


def test_fmt_tokens_units():
    from codexctl.web.charts import fmt_tokens
    cases = {0: "0", None: "0", 999: "999", 1000: "1K", 1234: "1.23K", 12345: "12.3K", 999_499: "999K",
             999_500: "1M", 12_340_000: "12.3M", 5_678_900_000: "5.68B", 2 * 10**12: "2T"}
    for n, want in cases.items():
        assert fmt_tokens(n) == want, n


def test_daily_comparison_geometry_and_values():
    from codexctl.web.charts import comparison_series

    daily = [
        {"day": "2026-11-15", "values": [0]},
        {"day": "2026-11-16", "values": [50]},
        {"day": "2026-11-17", "values": [100]},
    ]
    series = comparison_series(daily, ["每日用量"], 100)[0]
    assert series["path"] == "M70.0,175.0 L495.0,105.0 L920.0,35.0"
    assert [day["x"] for day in daily] == [70.0, 495.0, 920.0]
    assert [(point["value"], point["day"]) for point in series["points"]] == [
        (0, "2026-11-15"), (50, "2026-11-16"), (100, "2026-11-17"),
    ]


def test_daily_comparison_empty_single_and_zero_values():
    from codexctl.web.charts import comparison_series

    empty = comparison_series([], ["每日用量"], 0)[0]
    assert empty["path"] == "" and empty["points"] == []
    single = comparison_series([{"day": "2026-11-17", "values": [0]}], ["每日用量"], 0)[0]
    assert single["path"] == "M70.0,175.0"
    assert single["points"] == [{"x": 70.0, "y": 175.0, "value": 0, "day": "2026-11-17"}]
    zero = comparison_series([
        {"day": "2026-11-16", "values": [0]}, {"day": "2026-11-17", "values": [0]},
    ], ["每日用量"], 0)[0]
    assert zero["path"] == "M70.0,175.0 L920.0,175.0"
