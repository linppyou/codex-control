from __future__ import annotations

import hashlib
import io
import os
import re
import sqlite3
import stat
import tempfile
import zipfile
import zlib
from pathlib import Path
from uuid import uuid4

import codexctl.db as db
import codexctl.events as events
from codexctl.context import Context
from codexctl.dispatch.repository import get, list_skills

KINDS = {"dispatcher": "分发 Skill", "migration": "迁移 Skill"}
MAX_UPLOAD = 8 * 1024 * 1024
_MAX_EXPANDED = 64 * 1024 * 1024
_MAX_MEMBERS = 4096


class SkillError(ValueError):
    pass


def _package(archive: zipfile.ZipFile) -> tuple[str, str, list[tuple[zipfile.ZipInfo, tuple[str, ...]]]]:
    members = archive.infolist()
    if len(members) > _MAX_MEMBERS or sum(item.file_size for item in members) > _MAX_EXPANDED:
        raise SkillError("ZIP 包解压后超过 64 MiB 或包含过多文件")
    files: dict[tuple[str, ...], zipfile.ZipInfo] = {}
    paths: set[tuple[str, ...]] = set()
    for item in members:
        parts = tuple(item.filename.rstrip("/").split("/"))
        mode = stat.S_IFMT(item.external_attr >> 16)
        if (
            parts in paths or any(part in {"", ".", ".."} for part in parts)
            or any(not char.isprintable() or char in "\\:" for char in item.orig_filename)
            or mode not in (0, stat.S_IFDIR if item.is_dir() else stat.S_IFREG)
            or item.flag_bits & 1
        ):
            raise SkillError("ZIP 包含重复路径、非法路径、链接或加密文件")
        paths.add(parts)
        if not item.is_dir():
            files[parts] = item
    if any(parts[:n] in files for parts in paths for n in range(1, len(parts))):
        raise SkillError("ZIP 中的文件与目录路径冲突")

    entries = [parts for parts in files if parts[-1] == "SKILL.md"]
    if entries:
        depth = min(map(len, entries))
        entries = [parts for parts in entries if len(parts) == depth]
    if len(entries) != 1:
        raise SkillError("每个 ZIP 必须包含一个根 Skill 的 SKILL.md")
    entry = entries[0]
    if files[entry].file_size > 128 * 1024:
        raise SkillError("SKILL.md 超过 128 KiB")
    markdown = archive.read(files[entry]).decode("utf-8-sig")
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != "---" or "---" not in lines[1:]:
        raise SkillError("SKILL.md 缺少元数据")
    header = "\n".join(lines[1:lines.index("---", 1)])
    name = re.search(r"^name:[ \t]*([^\n]+)$", header, re.MULTILINE)
    version = re.search(r"^[ \t]*version:[ \t]*([^\n]+)$", header, re.MULTILINE)
    slug = name[1].strip().strip("\"'") if name else ""
    release = version[1].strip().strip("\"'") if version else ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        raise SkillError("SKILL.md 的 name 须为小写英文、数字和连字符")
    if release and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._+-]{0,63}", release):
        raise SkillError("SKILL.md 的 version 格式无效")
    root = entry[:-1]
    return slug, release, [(item, path[len(root):]) for path, item in files.items() if path[:len(root)] == root]


def inspect(data: bytes) -> tuple[str, str]:
    if not data or len(data) > MAX_UPLOAD:
        raise SkillError("请上传不超过 8 MiB 的 Skill ZIP 包")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            name, version, files = _package(archive)
            for item, _ in files:
                with archive.open(item) as source:
                    while source.read(64 * 1024):
                        pass
            return name, version
    except SkillError:
        raise
    except (zipfile.BadZipFile, UnicodeError, RuntimeError, OSError, ValueError, EOFError, zlib.error) as exc:
        raise SkillError("无法读取 ZIP 或 SKILL.md 文件") from exc


def unpack(data: bytes, destination: Path) -> Path:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        slug, _, files = _package(archive)
        root = destination / slug
        root.mkdir(parents=True)
        for item, parts in files:
            path = root.joinpath(*parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, path.open("xb") as output:
                while block := source.read(64 * 1024):
                    output.write(block)
            if (item.external_attr >> 16) & stat.S_IXUSR:
                path.chmod(0o700)
        return root / "SKILL.md"


def sync_user_skills(ctx: Context) -> None:
    """在数据库事务内发布完整目录，并将本机普通用户链接到管理员版本。"""
    catalog = ctx.conn.execute("SELECT * FROM dispatch_skills ORDER BY name").fetchall()
    digest = hashlib.sha256()
    for skill in catalog:
        digest.update(f"{skill['name']}:{skill['sha256']}\n".encode())
    releases = ctx.settings.state_dir / "skill-releases"
    releases.mkdir(mode=0o755, exist_ok=True)
    releases.chmod(0o755)
    release = releases / digest.hexdigest()
    if not release.exists():
        with tempfile.TemporaryDirectory(dir=releases) as temporary:
            staged = Path(temporary) / "skills"
            staged.mkdir(mode=0o755)
            staged.chmod(0o755)
            for skill in catalog:
                if hashlib.sha256(skill["archive"]).hexdigest() != skill["sha256"]:
                    raise SkillError("Skill ZIP 校验失败")
                unpack(skill["archive"], staged)
            for path in staged.rglob("*"):
                mode = path.stat().st_mode
                path.chmod(0o755 if path.is_dir() or mode & stat.S_IXUSR else 0o644)
            staged.rename(release)
    # Keep older releases available to sessions that already resolved their paths.
    target = ctx.settings.state_dir / "skills"
    temporary_link = ctx.settings.state_dir / ".skills.tmp"
    temporary_link.unlink(missing_ok=True)
    temporary_link.symlink_to(release, target_is_directory=True)
    os.replace(temporary_link, target)
    for user in ctx.list_users(max(1, ctx.settings.min_uid)):
        if user.name not in {"root", "admin"} and user.home.is_dir():
            ctx.publisher.link_skills(user, target)


def save(
    ctx: Context, skill_id: str | None, kind: str, label: str, data: bytes | None,
    metadata: tuple[str, str] | None,
) -> str:
    label = label.strip()
    if kind not in KINDS or len(label) > 80 or any(not char.isprintable() for char in label):
        raise SkillError("请输入有效的 Skill 类型和显示名称")
    with db.transaction(ctx.conn):
        existing = get(ctx.conn, skill_id) if skill_id else None
        if skill_id and existing is None:
            raise SkillError("Skill 已删除")
        if data is None and existing is None:
            raise SkillError("请选择 Skill ZIP 包")
        name, version = metadata if metadata is not None else (existing["name"], existing["version"])
        if data is not None and existing is not None and existing["name"] != name:
            raise SkillError("更新包的 Skill name 必须与原包一致")
        label = label or name
        digest = hashlib.sha256(data).hexdigest() if data is not None else None
        if existing is None:
            skill_id = uuid4().hex
            try:
                ctx.conn.execute(
                    "INSERT INTO dispatch_skills VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (skill_id, kind, name, version, label, digest, data),
                )
            except sqlite3.IntegrityError as exc:
                raise SkillError("同名 Skill 已存在，请编辑现有条目") from exc
        elif data is None:
            ctx.conn.execute("UPDATE dispatch_skills SET kind=?, label=? WHERE id=?", (kind, label, skill_id))
        else:
            ctx.conn.execute(
                "UPDATE dispatch_skills SET kind=?, label=?, version=?, sha256=?, archive=? WHERE id=?",
                (kind, label, version, digest, data, skill_id),
            )
        events.audit(ctx.conn, "admin", "dispatch.skill_save", skill_id, f"{name} {version}", now=ctx.now())
        return skill_id


def delete(ctx: Context, skill_id: str) -> None:
    with db.transaction(ctx.conn):
        if ctx.conn.execute("DELETE FROM dispatch_skills WHERE id = ?", (skill_id,)).rowcount:
            events.audit(ctx.conn, "admin", "dispatch.skill_delete", skill_id, now=ctx.now())
