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
