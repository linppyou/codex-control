from __future__ import annotations

import errno
import os
import stat
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
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise VaultError(f"{path} 是符号链接，拒绝读取") from e
        raise VaultError(f"无法打开 {path}: {e}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise VaultError(f"{path} 不是普通文件")
        if st.st_uid != os.geteuid():
            raise VaultError(f"{path} 属主必须是当前用户")
        if st.st_mode & 0o077:
            raise VaultError(f"{path} 权限过宽，必须只有属主可读（chmod 400）")
        key = os.read(fd, 64)
    finally:
        os.close(fd)
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
        if len(blob) < _NONCE_LEN + 16:
            raise VaultError("解密失败：数据长度不足")
        try:
            return self._aead.decrypt(blob[:_NONCE_LEN], blob[_NONCE_LEN:], aad)
        except InvalidTag as e:
            raise VaultError("解密失败：主密钥不匹配或数据被篡改") from e
