from pathlib import Path

import pytest

from codexctl.config import Settings, load_settings


def test_defaults_when_file_missing(tmp_path):
    s = load_settings(tmp_path / "missing.toml")
    assert s == Settings()
    assert s.db_path == Path("/var/lib/codexctl/codexctl.db")
    assert s.accounts_dir == Path("/var/lib/codexctl/accounts")
    assert s.staging_dir == Path("/var/lib/codexctl/staging")


def test_overrides_and_path_conversion(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('state_dir = "/srv/cx"\nmin_uid = 2000\n')
    s = load_settings(cfg)
    assert s.state_dir == Path("/srv/cx")
    assert s.min_uid == 2000
    assert s.staging_dir == Path("/srv/cx/staging")


def test_unknown_key_rejected(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("nope = 1\n")
    with pytest.raises(ValueError, match="unknown config key: nope"):
        load_settings(cfg)


def test_quota_poll_seconds_default():
    assert Settings().quota_poll_seconds == 600
