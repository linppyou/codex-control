from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import SSHCertificate, SSHCertificateBuilder, SSHCertificateType

from .config import Settings
from .sysusers import SysUser, valid_name

KEY_NAME = "id_ed25519"
PUBLIC_NAME = f"{KEY_NAME}.pub"
CERT_NAME = f"{KEY_NAME}-cert.pub"
PRINCIPAL = b"codexctl-site-root"
CERT_EXPIRY = (1 << 64) - 1


def _load_ca(path: Path) -> Ed25519PrivateKey:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError("站点 SSH CA 私钥必须是仅 root 可访问的普通文件")
        try:
            key = serialization.load_ssh_private_key(source.read(), password=None)
        except (TypeError, UnsupportedAlgorithm, ValueError) as exc:
            raise ValueError("无法读取站点 SSH CA 私钥") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("站点 SSH CA 私钥必须是 Ed25519 密钥")
    return key


def _openssh_key(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)


def _current_certificate(path: Path, public_key: Ed25519PublicKey, ca: Ed25519PrivateKey, key_id: bytes) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o444:
        raise ValueError(f"现有站点 SSH 证书的属主或权限不符：{path}")
    try:
        certificate = serialization.load_ssh_public_identity(path.read_bytes().strip())
        if not isinstance(certificate, SSHCertificate):
            return False
        certificate.verify_cert_signature()
    except (InvalidSignature, UnsupportedAlgorithm, ValueError):
        return False
    certified_key = certificate.public_key()
    signing_key = certificate.signature_key()
    return (
        certificate.type == SSHCertificateType.USER
        and isinstance(certified_key, Ed25519PublicKey)
        and isinstance(signing_key, Ed25519PublicKey)
        and _openssh_key(certified_key) == _openssh_key(public_key)
        and _openssh_key(signing_key) == _openssh_key(ca.public_key())
        and certificate.key_id == key_id
        and certificate.valid_principals == [PRINCIPAL]
        and certificate.valid_after == 0
        and certificate.valid_before == CERT_EXPIRY
        and not certificate.critical_options
        and certificate.extensions == {b"permit-pty": b""}
    )


def _ensure_certificate(target: Path, user: SysUser, public_line: str, ca: Ed25519PrivateKey) -> None:
    public_key = serialization.load_ssh_public_key(public_line.encode("ascii"))
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("站点 SSH 用户公钥必须是 Ed25519 密钥")
    key_id = f"{user.name}:{user.uid}".encode("ascii")
    path = target / CERT_NAME
    if _current_certificate(path, public_key, ca, key_id):
        return
    certificate = (
        SSHCertificateBuilder()
        .public_key(public_key)
        .serial(secrets.randbits(64))
        .type(SSHCertificateType.USER)
        .key_id(key_id)
        .valid_principals([PRINCIPAL])
        .valid_after(0)
        .valid_before(CERT_EXPIRY)
        .add_extension(b"permit-pty", b"")
        .sign(ca)
    )
    with NamedTemporaryFile(dir=target, delete=False) as staged:
        temporary_path = Path(staged.name)
        try:
            staged.write(certificate.public_bytes() + b"\n")
            staged.close()
            temporary_path.chmod(0o444)
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def _check_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError(f"站点 SSH 目录必须由 root 管理：{path}")


def _is_provisioned(path: Path, user: SysUser) -> bool:
    try:
        _check_directory(path)
    except FileNotFoundError:
        return False
    for name, owner, mode in ((KEY_NAME, user.uid, 0o400), (PUBLIC_NAME, 0, 0o444)):
        info = (path / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or stat.S_IMODE(info.st_mode) != mode:
            raise ValueError(f"现有站点 SSH 文件的属主或权限不符：{path / name}")
    return True


def provision(settings: Settings, user: SysUser) -> str:
    if os.geteuid() != 0:
        raise ValueError("开通站点 SSH 需要 root 权限")
    if user.uid < max(1, settings.min_uid) or user.name in settings.hidden_users or not valid_name(user.name):
        raise ValueError(f"该用户不能开通站点 SSH：{user.name}")

    ca = _load_ca(settings.site_ssh_ca_key) if settings.site_ssh_ca_key is not None else None
    base = settings.state_dir / "site-access"
    _check_directory(settings.state_dir)
    base.mkdir(mode=0o711, exist_ok=True)
    _check_directory(base)
    base.chmod(0o711)
    target = base / f"{user.name}-{user.uid}"
    if not _is_provisioned(target, user):
        _create_key(base, target, user)
    public_line = (target / PUBLIC_NAME).read_text(encoding="ascii").strip()
    if ca is not None:
        _ensure_certificate(target, user, public_line, ca)
    return public_line


def _create_key(base: Path, target: Path, user: SysUser) -> None:
    key = Ed25519PrivateKey.generate()
    with TemporaryDirectory(dir=base) as directory:
        staging = Path(directory)
        private_file = staging / KEY_NAME
        private_file.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ))
        private_file.chmod(0o400)
        os.chown(private_file, user.uid, user.gid)
        public_file = staging / PUBLIC_NAME
        public_key = _openssh_key(key.public_key())
        public_file.write_bytes(public_key + f" codexctl-site-{user.name}-{user.uid}\n".encode("ascii"))
        public_file.chmod(0o444)
        staging.chmod(0o711)
        try:
            staging.rename(target)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not _is_provisioned(target, user):
                raise
