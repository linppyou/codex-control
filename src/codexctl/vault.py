from __future__ import annotations

import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12


class VaultError(Exception):
    pass


def generate_key_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "wb") as f:
        f.write(AESGCM.generate_key(bit_length=256))


def load_key(path: Path) -> bytes:
    if path.stat().st_mode & 0o077:
        raise VaultError(f"{path} 权限过宽，必须只有属主可读（chmod 400）")
    key = path.read_bytes()
    if len(key) != 32:
        raise VaultError(f"{path} 不是 32 字节的主密钥")
    return key


class Vault:
    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(_NONCE_LEN)
        return nonce + self._aead.encrypt(nonce, plaintext, aad)

    def decrypt(self, blob: bytes, aad: bytes) -> bytes:
        try:
            return self._aead.decrypt(blob[:_NONCE_LEN], blob[_NONCE_LEN:], aad)
        except InvalidTag as e:
            raise VaultError("解密失败：主密钥不匹配或数据被篡改") from e
