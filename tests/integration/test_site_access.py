import os
import pwd
import stat

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from codexctl.config import Settings
from codexctl.site_access import CERT_NAME, KEY_NAME, PRINCIPAL, provision, shared_key
from codexctl.sysusers import SysUser

pytestmark = [pytest.mark.integration, pytest.mark.skipif(os.geteuid() != 0, reason="需要 root")]


def test_site_ssh_key_and_certificate_are_repeatable(tmp_path):
    entry = pwd.getpwnam("alice")
    user = SysUser(entry.pw_name, entry.pw_uid, entry.pw_gid, tmp_path / "alice")
    state = tmp_path / "state"
    state.mkdir()
    ca = Ed25519PrivateKey.generate()
    ca_file = tmp_path / "site-ca"
    ca_file.write_bytes(ca.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption(),
    ))
    ca_file.chmod(0o600)
    settings = Settings(state_dir=state, site_ssh_enabled=True, site_ssh_ca_key=ca_file)

    public_key = provision(settings, user)
    directory = state / "site-access" / f"alice-{user.uid}"
    private_file = directory / KEY_NAME
    certificate_file = directory / CERT_NAME
    private_bytes = private_file.read_bytes()
    certificate_bytes = certificate_file.read_bytes()
    assert private_file.stat().st_uid == user.uid
    assert stat.S_IMODE(private_file.stat().st_mode) == 0o400
    certificate = serialization.load_ssh_public_identity(certificate_bytes.strip())
    certificate.verify_cert_signature()
    assert certificate.valid_principals == [PRINCIPAL]
    assert certificate.key_id == f"alice:{user.uid}".encode()
    assert provision(settings, user) == public_key
    assert private_file.read_bytes() == private_bytes
    assert certificate_file.read_bytes() == certificate_bytes

    ca_file.chmod(0o644)
    with pytest.raises(ValueError, match="仅 root"):
        provision(settings, user)


def test_shared_key_migrates_existing_user_and_is_repeatable(tmp_path):
    entry = next(item for item in pwd.getpwall() if 1000 <= item.pw_uid < 65534)
    user = SysUser(entry.pw_name, entry.pw_uid, entry.pw_gid, tmp_path / "alice")
    state = tmp_path / "state"
    state.mkdir()
    provision(Settings(state_dir=state), user)
    old_key = (state / "site-access" / f"{user.name}-{user.uid}" / KEY_NAME).read_bytes()
    settings = Settings(state_dir=state, site_ssh_enabled=True, site_ssh_shared=True)
    public = provision(settings, user)
    directory = state / "site-access" / f"{user.name}-{user.uid}"
    assert (directory / KEY_NAME).read_bytes() == shared_key(settings).read_bytes()
    assert (directory / KEY_NAME).read_bytes() != old_key
    assert (directory / KEY_NAME).stat().st_uid == user.uid
    assert stat.S_IMODE((directory / KEY_NAME).stat().st_mode) == 0o400
    assert not (directory / CERT_NAME).exists()
    assert provision(settings, user) == public
    other = SysUser("second", user.uid, user.gid, tmp_path / "second")
    assert provision(settings, other) == public
    assert (state / "site-access" / f"second-{user.uid}" / KEY_NAME).read_bytes() == shared_key(settings).read_bytes()
