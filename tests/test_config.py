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


def test_codex_install_overrides(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        'codex_wrapper = "/srv/wrapper"\n'
        'codex_requirements = "/srv/requirements.toml"\n'
        'codex_version = "9.9.9"\n'
        'codex_package_url = "https://mirror/{version}/{arch}.tgz"\n'
        "codex_auto_install = false\n"
        "codex_install_timeout = 42.0\n"
    )
    s = load_settings(cfg)
    assert s.codex_wrapper == Path("/srv/wrapper")
    assert s.codex_requirements == Path("/srv/requirements.toml")
    assert s.codex_version == "9.9.9"
    assert s.codex_package_url == "https://mirror/{version}/{arch}.tgz"
    assert s.codex_auto_install is False
    assert s.codex_install_timeout == 42.0
