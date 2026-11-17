from __future__ import annotations

import ipaddress
import re
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

_HOSTNAME = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{1,63}")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_REDIRECTS = {301, 302, 303, 307, 308}
_PREFERENCE = {"up": 4, "http_only": 3, "redirect": 2, "restricted": 1, "down": 0}
_REQUEST_TIMEOUT = 10
_SITE_TIMEOUT = 24
UNCONFIRMED_REASONS = {"timeout", "connect", "probe_error"}


@dataclass(frozen=True)
class Probe:
    state: str
    code: int = 0
    reason: str = ""
    entry_url: str = ""
    final_host: str = ""


def hostname(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        host = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    suffix = host.rpartition(".")[2]
    if (
        _HOSTNAME.fullmatch(host) and _LABEL.fullmatch(suffix) and not suffix.isdigit()
        and suffix not in {"local", "localhost", "localdomain", "internal"}
    ):
        return host
    return None


def public_ip(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            return False
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def _resolve(host: str, timeout: float) -> tuple[list[str], str]:
    try:
        proc = subprocess.run(
            ["getent", "-s", "dns", "ahosts", host], capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [], "timeout"
    except OSError:
        return [], "probe_error"
    if proc.returncode == 2:
        return [], "dns"
    if proc.returncode != 0:
        return [], "probe_error"
    addresses = list(dict.fromkeys(line.split()[0] for line in proc.stdout.splitlines() if line.strip()))
    try:
        if addresses and all(public_ip(addr) for addr in addresses):
            return addresses, ""
    except ValueError:
        pass
    return [], "address" if addresses else "dns"


def _request(url: str, host: str, port: int, addresses: list[str], timeout: float) -> tuple[int, str, dict]:
    peers = ",".join(f"[{addr}]" if ":" in addr else addr for addr in addresses)
    try:
        proc = subprocess.run(
            [
                "curl", "--disable", "--silent", "--noproxy", "*", "--globoff",
                "--proto", "=http,https", "--connect-timeout", str(min(timeout, 3)),
                "--max-time", str(timeout), "--max-filesize", "262144",
                "--resolve", f"{host}:{port}:{peers}", "--dump-header", "-",
                "--output", "/dev/null", "--write-out", "\n%{http_code}", "--", url,
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout + 0.5,
        )
    except subprocess.TimeoutExpired:
        return 0, "timeout", {}
    except OSError:
        return 0, "probe_error", {}
    headers_text, _, code_text = proc.stdout.rpartition("\n")
    code = int(code_text) if code_text.isascii() and code_text.isdigit() else 0
    if not code:
        reason = {7: "connect", 28: "timeout", 35: "tls", 51: "tls", 60: "tls"}.get(proc.returncode, "connect")
        return 0, reason, {}
    headers = {}
    for line in headers_text.splitlines():
        if line.startswith("HTTP/"):
            headers.clear()
        elif ":" in line:
            name, value = line.split(":", 1)
            headers[name.lower()] = value.strip()
    return code, "", headers


def _location(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    host = hostname(parsed.hostname)
    port = {"http": 80, "https": 443}.get(parsed.scheme)
    if (
        not host or host != parsed.hostname or not port or parsed.username is not None
        or parsed.port not in (None, port) or "\\" in parsed.netloc
        or any(ord(char) < 32 or ord(char) == 127 for char in url)
    ):
        raise ValueError("unsupported probe URL")
    return host, port


def _walk(entry: str, domains: tuple[str, ...], deadline: float, origin: str | None, cache: dict) -> Probe:
    url, code, host = entry, 0, ""
    visited = set()
    for _ in range(6):
        try:
            host, port = _location(url)
        except ValueError:
            return Probe("down", code, "redirect_address", entry, host)
        if origin and host not in domains:
            return Probe("redirect", code, "", entry, host)
        if url in visited:
            return Probe("down", code, "redirect_loop", entry, host)
        visited.add(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return Probe("down", 0, "timeout", entry, host)
        if origin:
            addresses, reason = [origin], ""
        else:
            if host not in cache:
                cache[host] = _resolve(host, min(remaining, 3))
            addresses, reason = cache[host]
        if not addresses:
            return Probe("down", 0, reason, entry, host)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return Probe("down", 0, "timeout", entry, host)
        code, reason, headers = _request(url, host, port, addresses, min(remaining, _REQUEST_TIMEOUT))
        if code in _REDIRECTS and headers.get("location"):
            try:
                url = urljoin(url, headers["location"]).split("#", 1)[0]
            except ValueError:
                return Probe("down", code, "redirect_address", entry, host)
            continue
        if 200 <= code < 300:
            state = "redirect" if host not in domains else ("up" if port == 443 else "http_only")
            return Probe(state, code, "", entry, host)
        if code in (401, 403, 429):
            reason = "challenge" if headers.get("cf-mitigated") == "challenge" else "http"
            return Probe("restricted", code, reason, entry, host)
        return Probe("down", code, reason or "http", entry, host)
    return Probe("down", code, "redirect_limit", entry, host)


def probe(domains: tuple[str, ...], origin: str | None = None) -> Probe:
    deadline = time.monotonic() + _SITE_TIMEOUT
    attempts = []
    cache = {}
    for host in domains:
        result = _walk(f"https://{host}/", domains, deadline, origin, cache)
        if result.state == "up":
            return result
        attempts.append(result)
        if result.reason in ("dns", "address") and result.final_host == host:
            continue
        result = _walk(f"http://{host}/", domains, deadline, origin, cache)
        if result.state == "up":
            return result
        attempts.append(result)
    return max(
        attempts,
        key=lambda result: (_PREFERENCE[result.state], result.reason in UNCONFIRMED_REASONS, bool(result.code)),
    )
