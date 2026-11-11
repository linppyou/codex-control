from codexctl import db, events


def test_audit_inserts_row():
    conn = db.connect(":memory:")
    events.audit(conn, "cli", "assign", "alice", "acc1", now=10)
    row = conn.execute("SELECT * FROM audit_log").fetchone()
    assert (row["ts"], row["actor"], row["action"], row["target"], row["detail"]) == (10, "cli", "assign", "alice", "acc1")


def test_raise_alert_updates_open_alert_instead_of_duplicating():
    conn = db.connect(":memory:")
    events.raise_alert(conn, "warning", "quota_high", "acc1", "81%", now=1)
    events.raise_alert(conn, "critical", "quota_high", "acc1", "97%", now=2)
    rows = events.open_alerts(conn)
    assert len(rows) == 1
    assert (rows[0]["level"], rows[0]["message"], rows[0]["ts"]) == ("critical", "97%", 2)


def test_resolve_then_raise_creates_new_alert():
    conn = db.connect(":memory:")
    events.raise_alert(conn, "warning", "quota_high", "acc1", "81%", now=1)
    events.resolve_alert(conn, "quota_high", "acc1", now=2)
    assert events.open_alerts(conn) == []
    events.raise_alert(conn, "warning", "quota_high", "acc1", "85%", now=3)
    assert len(events.open_alerts(conn)) == 1
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 2


def test_resolve_missing_alert_is_noop():
    conn = db.connect(":memory:")
    events.resolve_alert(conn, "quota_high", "nope", now=1)
    assert events.open_alerts(conn) == []
