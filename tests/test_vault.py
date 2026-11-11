import stat

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from codexctl.vault import Vault, VaultError, generate_key_file, load_key


def test_roundtrip():
    v = Vault(AESGCM.generate_key(bit_length=256))
    blob = v.encrypt(b"secret", b"acc1")
    assert b"secret" not in blob
    assert v.decrypt(blob, b"acc1") == b"secret"


def test_wrong_aad_fails():
    v = Vault(AESGCM.generate_key(bit_length=256))
    blob = v.encrypt(b"secret", b"acc1")
    with pytest.raises(VaultError, match="解密失败"):
        v.decrypt(blob, b"acc2")


def test_wrong_key_fails():
    blob = Vault(AESGCM.generate_key(bit_length=256)).encrypt(b"secret", b"a")
    with pytest.raises(VaultError):
        Vault(AESGCM.generate_key(bit_length=256)).decrypt(blob, b"a")


def test_generate_and_load_key(tmp_path):
    path = tmp_path / "etc" / "master.key"
    generate_key_file(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert len(load_key(path)) == 32


def test_generate_refuses_to_overwrite(tmp_path):
    path = tmp_path / "master.key"
    generate_key_file(path)
    with pytest.raises(FileExistsError):
        generate_key_file(path)


def test_load_key_rejects_loose_permissions(tmp_path):
    path = tmp_path / "master.key"
    path.write_bytes(b"x" * 32)
    path.chmod(0o644)
    with pytest.raises(VaultError, match="权限过宽"):
        load_key(path)


def test_load_key_rejects_wrong_length(tmp_path):
    path = tmp_path / "master.key"
    path.write_bytes(b"short")
    path.chmod(0o400)
    with pytest.raises(VaultError, match="32 字节"):
        load_key(path)
