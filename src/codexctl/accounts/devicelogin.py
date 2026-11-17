from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from codexctl.accounts.publisher import LABEL_RE
from codexctl.config import Settings

log = logging.getLogger(__name__)
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_URL_RE = re.compile(r"https://\S+")
_ENV_KEYS = (
    "PATH HOME LANG LC_ALL LC_CTYPE TERM http_proxy https_proxy HTTP_PROXY HTTPS_PROXY "
    "NO_PROXY no_proxy SSL_CERT_FILE SSL_CERT_DIR"
).split()


ALLOWED_HOSTS = ("auth.openai.com",)


class DeviceLoginError(ValueError):
    pass


def _openai_url(text: str) -> str | None:
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:)]}'\"")
        if "\\" in url or "@" in url:
            continue
        try:
            u = urlparse(url)
            host = u.hostname
        except ValueError:
            continue
        if u.scheme != "https" or host not in ALLOWED_HOSTS or u.port is not None:
            continue
        return f"https://{host}{u.path}" + (f"?{u.query}" if u.query else "")
    return None


_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")

LOGIN_TIMEOUT = 15 * 60
RECORD_TTL = 10 * 60
_ACTIVE = ("pending", "waiting")


@dataclass
class _Login:
    label: str
    mode: str
    home: Path
    proc: subprocess.Popen
    started_at: float
    state: str = "pending"
    url: str | None = None
    code: str | None = None
    error: str | None = None
    finished_at: float | None = None
    taken: bool = False
    lines: deque = field(default_factory=lambda: deque(maxlen=50))


class DeviceLoginManager:
    def __init__(self, settings: Settings, clock=time.time):
        self._settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._logins: dict[str, _Login] = {}
        # 只清理超过 15 分钟的残留：CLI `account add` 可能正在并发使用 staging
        staging = settings.staging_dir
        cutoff = clock() - LOGIN_TIMEOUT
        if staging.is_dir():
            for entry in staging.iterdir():
                try:
                    if entry.lstat().st_mtime >= cutoff:
                        continue
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink()
                except OSError:
                    pass

    def start(self, label: str, mode: str) -> str:
        if mode not in ("add", "relogin"):
            raise DeviceLoginError("mode 只能是 add 或 relogin")
        if not LABEL_RE.fullmatch(label):
            raise DeviceLoginError("账号标签格式不合法")
        self.sweep()
        with self._lock:
            if any(l.label == label and l.state in _ACTIVE for l in self._logins.values()):
                raise DeviceLoginError("该账号已有进行中的登录")
            staging = self._settings.staging_dir
            staging.mkdir(parents=True, exist_ok=True)
            staging.chmod(0o700)
            home = Path(tempfile.mkdtemp(prefix=f"{label}-", dir=staging))
            try:
                proc = subprocess.Popen(
                    [str(self._settings.codex_bin), "login", "--device-auth"],
                    env={**{k: os.environ[k] for k in _ENV_KEYS if k in os.environ}, "CODEX_HOME": str(home)},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    encoding="utf-8",
                    errors="replace",
                    start_new_session=True,
                )
            except OSError as e:
                shutil.rmtree(home, ignore_errors=True)
                raise DeviceLoginError(f"无法运行 codex：{e}") from e
            login_id = secrets.token_urlsafe(16)
            login = _Login(label=label, mode=mode, home=home, proc=proc, started_at=self._clock())
            self._logins[login_id] = login
        threading.Thread(target=self._read, args=(login,), daemon=True).start()
        return login_id

    def _read(self, login: _Login) -> None:
        try:
            for raw in login.proc.stdout:
                line = _ANSI_RE.sub("", raw).rstrip("\r\n")
                with self._lock:
                    login.lines.append(line)
                    if login.url is None:
                        login.url = _openai_url(line)
                    if login.code is None and (m := _CODE_RE.search(line)):
                        login.code = m.group(0)
                    if login.state == "pending" and login.url and login.code:
                        login.state = "waiting"
        except Exception:
            pass
        finally:
            rc = -1
            try:
                rc = login.proc.wait()
            except Exception:
                pass
            try:
                login.proc.stdout.close()
            except Exception:
                pass
            with self._lock:
                if login.state in _ACTIVE:  # 未被取消/过期
                    login.finished_at = self._clock()
                    if rc == 0 and (login.home / "auth.json").exists():
                        login.state = "succeeded"
                    else:
                        login.state = "failed"
                        login.error = f"codex login 退出码 {rc}"
                        shutil.rmtree(login.home, ignore_errors=True)

    def status(self, login_id: str) -> dict | None:
        self.sweep()
        with self._lock:
            l = self._logins.get(login_id)
            if l is None:
                return None
            return {
                "label": l.label,
                "mode": l.mode,
                "state": l.state,
                "url": l.url,
                "code": l.code,
                "error": l.error,
                "started_at": l.started_at,
            }

    def finished_ids(self) -> list[str]:
        with self._lock:
            return [i for i, l in self._logins.items() if l.state == "succeeded" and not l.taken]

    def take_result(self, login_id: str) -> bytes:
        with self._lock:
            l = self._logins.get(login_id)
            if l is None or l.state != "succeeded" or l.taken:
                raise DeviceLoginError("没有可取的登录结果")
            l.taken = True
            try:
                return (l.home / "auth.json").read_bytes()
            except OSError as e:
                raise DeviceLoginError("登录结果文件不可读") from e
            finally:
                shutil.rmtree(l.home, ignore_errors=True)

    @staticmethod
    def _signal(l: _Login, sig: int) -> None:
        try:
            os.killpg(l.proc.pid, sig)
        except ProcessLookupError:
            pass

    def close(self) -> None:
        with self._lock:
            for l in self._logins.values():
                if l.state in _ACTIVE:
                    self._stop(l, "cancelled")

    def _stop(self, l: _Login, state: str) -> None:
        """终止进程并清理目录（调用方持锁）。"""
        l.state = state
        l.finished_at = self._clock()
        if l.proc.poll() is None:
            self._signal(l, signal.SIGTERM)
            try:
                l.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._signal(l, signal.SIGKILL)
                l.proc.wait()
        shutil.rmtree(l.home, ignore_errors=True)

    def cancel(self, login_id: str) -> None:
        with self._lock:
            l = self._logins.get(login_id)
            if l is None or l.state not in _ACTIVE:
                return
            self._stop(l, "cancelled")

    def sweep(self) -> None:
        now = self._clock()
        with self._lock:
            for login_id, l in list(self._logins.items()):
                if l.state in _ACTIVE:
                    if now - l.started_at > LOGIN_TIMEOUT:
                        self._stop(l, "expired")
                elif l.finished_at is not None and now - l.finished_at > RECORD_TTL:
                    if l.state == "succeeded" and not l.taken:
                        log.warning("丢弃了未取走的登录结果：%s", l.label)
                    shutil.rmtree(l.home, ignore_errors=True)
                    del self._logins[login_id]
