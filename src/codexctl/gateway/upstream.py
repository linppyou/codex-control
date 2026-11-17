from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import threading
import uuid
from collections import OrderedDict
from dataclasses import replace
from typing import AsyncIterator

import httpx
import zstandard
from starlette.concurrency import run_in_threadpool

import codexctl.accounts.refresher as refresher
import codexctl.accounts.repository as account_repo
import codexctl.accounts.service as accounts
import codexctl.db as db
import codexctl.gateway.storage as storage
from codexctl.accounts.authfile import AuthBundle, AuthFileError
from codexctl.context import Context
from codexctl.gateway.storage import GatewayError, Key
from codexctl.gateway.websocket import WebSocketPool
from codexctl.vault import Vault, VaultError

BASE = "https://chatgpt.com/backend-api/codex"
VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")
MAX_BODY = 16 * 1024 * 1024
MAX_CATALOG = 1024 * 1024
MAX_EVENT = 4 * 1024 * 1024
UNSUPPORTED_OUTPUT_LIMIT_FIELDS = ("max_completion_tokens", "max_tokens")
TERMINALS = {"response.completed", "response.failed", "response.incomplete", "error"}
RESPONSE_HEADERS = ("x-codex-model", "openai-model", "x-model-routing-upstream")
RESPONSES_LITE = "x-openai-internal-codex-responses-lite"
TURN_METADATA = "x-codex-turn-metadata"
SESSION_FIELDS = {
    "session_id", "thread_id", "turn_id", "window_id", "context_window_id", "parent_thread_id",
    "parent_turn_id", "root_turn_id", "forked_from_thread_id", "installation_id",
}


def adapt(payload: dict, key: Key, *, allow_previous: bool = False) -> tuple[dict, bool]:
    model = payload.get("model")
    if not isinstance(model, str) or not storage.MODEL_ID.fullmatch(model):
        raise GatewayError(400, "invalid_model", "A valid explicit model ID is required.")
    if model not in key.models:
        raise GatewayError(403, "model_not_allowed", "This model is unavailable for the API Key.")
    tier = payload.get("service_tier")
    if tier == "fast":
        tier = "priority"
    if tier not in (None, "auto", "default", "priority"):
        raise GatewayError(400, "unsupported_service_tier", "Unsupported service tier.")
    if tier == "priority" and not (key.allow_fast and key.models[model]):
        raise GatewayError(403, "service_tier_not_allowed", "Fast is unavailable for this model or API Key.")
    previous = payload.get("previous_response_id")
    if (previous is not None and (not allow_previous or not isinstance(previous, str) or not previous
                                 or len(previous) > 256)
            or payload.get("conversation") is not None or payload.get("background")):
        raise GatewayError(400, "unsupported_state", "Supply conversation items inline. Stored and background responses are unavailable.")
    for name in UNSUPPORTED_OUTPUT_LIMIT_FIELDS:
        if payload.get(name) is not None:
            raise GatewayError(400, "unsupported_output_limit", f"{name} is unavailable for Codex subscription models.")

    data = payload.copy()
    output_limit = data.get("max_output_tokens")
    if output_limit is not None and (type(output_limit) is not int or output_limit <= 0):
        raise GatewayError(400, "invalid_output_limit", "max_output_tokens must be a positive integer.")
    for field in (*UNSUPPORTED_OUTPUT_LIMIT_FIELDS, "metadata", "user", "conversation", "background"):
        data.pop(field, None)
    if previous is None:
        data.pop("previous_response_id", None)
    if tier == "priority":
        data["service_tier"] = tier
    else:
        data.pop("service_tier", None)
    stream = data.pop("stream", False)
    if type(stream) is not bool:
        raise GatewayError(400, "invalid_stream", "stream must be a boolean.")
    if data.get("instructions") is None:
        data["instructions"] = "You are a helpful assistant."
    if isinstance(data.get("input"), str):
        data["input"] = [{"role": "user", "content": [{"type": "input_text", "text": data["input"]}]}]
    data["stream"] = True
    data["store"] = False
    include = data.get("include")
    if include is None:
        include = []
    if not isinstance(include, list) or not all(isinstance(item, str) for item in include):
        raise GatewayError(400, "invalid_include", "include must be a list of strings.")
    data["include"] = list(dict.fromkeys([*include, "reasoning.encrypted_content"]))
    return data, stream


def scope_session(key: Key, payload: dict, incoming: httpx.Headers, vault: Vault, *, streaming: bool) -> dict:
    def scoped(value: str) -> str:
        namespaced = f"{key.account_pk}:{key.public_id}:{value}"
        return hashlib.sha256(namespaced.encode("utf-8", errors="surrogatepass")).hexdigest()

    def scope_fields(fields: dict):
        return {name: scoped(value) if name in SESSION_FIELDS and isinstance(value, str) else value
                for name, value in fields.items()}

    def scope_metadata(value):
        try:
            fields = json.loads(value) if isinstance(value, str) and len(value) <= 32768 else None
        except (ValueError, RecursionError):
            fields = None
        if isinstance(fields, dict):
            return json.dumps(scope_fields(fields), separators=(",", ":"), ensure_ascii=True)
        return None

    headers = {}
    session_id = incoming.get("session-id") or incoming.get("session_id")
    thread_id = incoming.get("thread-id") or incoming.get("thread_id")
    if session_id:
        headers["session-id"] = scoped(session_id)
    if thread_id:
        headers["thread-id"] = scoped(thread_id)
    for name in ("x-codex-window-id", "x-client-request-id"):
        if incoming.get(name):
            headers[name] = scoped(incoming[name])
    meta_header = scope_metadata(incoming.get(TURN_METADATA))
    if meta_header and len(meta_header) < 8000:
        headers[TURN_METADATA] = meta_header
    metadata = payload.get("client_metadata")
    if isinstance(metadata, dict):
        metadata = scope_fields(metadata)
        if TURN_METADATA in metadata:
            raw = scope_metadata(metadata.pop(TURN_METADATA))
            if raw:
                metadata[TURN_METADATA] = raw
        payload["client_metadata"] = metadata
    if streaming and incoming.get(RESPONSES_LITE, "").strip().lower() == "true":
        headers[RESPONSES_LITE] = "true"
    turn_state = incoming.get("x-codex-turn-state")
    if turn_state and len(turn_state) <= 8192:
        try:
            blob = base64.b64decode(turn_state, altchars=b"-_", validate=True)
            headers["x-codex-turn-state"] = vault.decrypt(blob, _turn_aad(key)).decode("ascii")
        except (ValueError, VaultError):
            pass
    if isinstance(payload.get("prompt_cache_key"), str):
        payload["prompt_cache_key"] = scoped(payload["prompt_cache_key"])
    elif "prompt_cache_key" not in payload and (session_id or thread_id):
        payload["prompt_cache_key"] = scoped(session_id or thread_id)
    if not session_id and isinstance(payload.get("prompt_cache_key"), str):
        headers["session-id"] = payload["prompt_cache_key"]
    return headers


def _turn_aad(key: Key) -> bytes:
    return f"codexctl-turn-state:{key.account_pk}:{key.public_id}".encode()


def response_headers(response: httpx.Response, key: Key, vault: Vault) -> dict:
    headers = {name: response.headers[name] for name in RESPONSE_HEADERS if name in response.headers}
    state = response.headers.get("x-codex-turn-state")
    if state and len(state) < 6000 and state.isascii():
        headers["x-codex-turn-state"] = base64.urlsafe_b64encode(
            vault.encrypt(state.encode("ascii"), _turn_aad(key))
        ).decode("ascii")
    return headers


async def bounded_json(response: httpx.Response, limit: int) -> dict:
    data = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=65536):
        data += chunk
        if len(data) > limit:
            raise GatewayError(502, "upstream_response_too_large", "The upstream response exceeded the size limit.")
    try:
        decoded = json.loads(data)
    except (ValueError, RecursionError):
        decoded = None
    if not isinstance(decoded, dict):
        raise GatewayError(502, "invalid_upstream_response", "The upstream did not return a JSON object.")
    return decoded


def upstream_status(response: httpx.Response) -> GatewayError:
    status = response.status_code
    if status == 429:
        return GatewayError(429, "upstream_rate_limited", "The bound account reached an upstream limit.",
                            response.headers.get("retry-after"))
    if status in (401, 403):
        return storage.unavailable()
    if status in (400, 404, 413, 422):
        return GatewayError(400 if status != 413 else 413, "upstream_request_rejected",
                            f"The Codex upstream rejected this request (HTTP {status}).")
    return GatewayError(502, "upstream_error", f"The Codex upstream returned HTTP {status}.")


class Gateway:
    def __init__(self, ctx: Context, *, transport: httpx.AsyncBaseTransport | None = None):
        self.ctx = ctx
        self.transport = transport
        self.client: httpx.AsyncClient | None = None
        self.catalog_tasks: dict[tuple[int, str], asyncio.Task] = {}
        self.refresh_tasks: dict[int, asyncio.Task[bool]] = {}
        self.credential_cache: OrderedDict[int, tuple[int, AuthBundle]] = OrderedDict()
        self.credential_lock = threading.Lock()
        self.websockets = WebSocketPool(ctx.settings)

    async def start(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(300, connect=self.ctx.settings.http_timeout, write=60),
            limits=httpx.Limits(max_connections=None,
                               max_keepalive_connections=self.ctx.settings.gateway_max_keepalive_connections,
                               keepalive_expiry=self.ctx.settings.gateway_keepalive_expiry),
            http2=self.ctx.settings.gateway_http2,
            follow_redirects=False, transport=self.transport, trust_env=False,
        )
        if self.ctx.settings.gateway_transport != "http":
            self.websockets.start()

    async def close(self):
        for task in self.catalog_tasks.values():
            task.cancel()
        await asyncio.gather(*self.catalog_tasks.values(), return_exceptions=True)
        await asyncio.gather(*self.refresh_tasks.values(), return_exceptions=True)
        await self.websockets.close()
        if self.client:
            await self.client.aclose()
        with self.credential_lock:
            self.credential_cache.clear()

    def _read_credentials(self, account_pk: int) -> tuple[account_repo.Account, int, AuthBundle]:
        with db.connection(self.ctx.settings.db_path) as conn:
            ctx = replace(self.ctx, conn=conn)
            conn.execute("BEGIN")
            acc = account_repo.get_account_by_pk(conn, account_pk)
            version = account_repo.secret_version(conn, account_pk)
            if acc is None or acc.status != "active" or version is None:
                with self.credential_lock:
                    self.credential_cache.pop(account_pk, None)
                raise storage.unavailable()
            try:
                with self.credential_lock:
                    cached = self.credential_cache.get(account_pk)
                    if cached and cached[0] == version:
                        self.credential_cache.move_to_end(account_pk)
                        return acc, version, cached[1]
                bundle = accounts.load_bundle(ctx, acc)
                with self.credential_lock:
                    if self.ctx.settings.gateway_credential_cache_size:
                        self.credential_cache[account_pk] = (version, bundle)
                        self.credential_cache.move_to_end(account_pk)
                        while len(self.credential_cache) > self.ctx.settings.gateway_credential_cache_size:
                            self.credential_cache.popitem(last=False)
                return acc, version, bundle
            except (AuthFileError, VaultError, LookupError):
                raise storage.unavailable() from None

    def _refresh_credentials(self, account: account_repo.Account, version: int) -> bool:
        with db.connection(self.ctx.settings.db_path) as conn:
            ctx = replace(self.ctx, conn=conn)
            try:
                return refresher.refresh_account(ctx, account, "gateway", observed_version=version)
            except (AuthFileError, VaultError, LookupError):
                raise storage.unavailable() from None

    async def _credentials(self, account_pk: int, rejected_version: int | None) -> tuple[str, str, int]:
        acc, version, bundle = await run_in_threadpool(self._read_credentials, account_pk)
        if acc.access_expires_at <= self.ctx.now() + 30 or version == rejected_version:
            task = self.refresh_tasks.get(account_pk)
            if task is None:
                task = asyncio.create_task(asyncio.to_thread(self._refresh_credentials, acc, version))
                self.refresh_tasks[account_pk] = task

                def remove(done):
                    self.refresh_tasks.pop(account_pk, None)
                    if not done.cancelled():
                        done.exception()

                task.add_done_callback(remove)
            if not await asyncio.shield(task):
                raise storage.unavailable()
            acc, version, bundle = await run_in_threadpool(self._read_credentials, account_pk)
        if acc.access_expires_at <= self.ctx.now():
            raise storage.unavailable()
        return acc.account_id, bundle.access_token, version

    async def send(self, account_pk: int, path: str, *, payload: dict | None = None, version: str | None = None,
                   headers: dict | None = None, metrics: dict | None = None) -> httpx.Response:
        assert self.client is not None
        rejected_version = None
        for attempt in range(2):
            started = time.monotonic()
            account_id, access_token, observed = await self._credentials(account_pk, rejected_version)
            if metrics is not None:
                metrics["credentials_ms"] = metrics.get("credentials_ms", 0) + round((time.monotonic() - started) * 1000, 2)
            upstream_headers = {
                "authorization": f"Bearer {access_token}",
                "chatgpt-account-id": account_id,
                "user-agent": f"codexctl/{self.ctx.settings.gateway_client_version}",
                "originator": "codexctl",
                "accept": "text/event-stream" if path == "/responses" else "application/json",
                **(headers or {}),
            }
            if payload is not None and self.ctx.settings.gateway_transport != "http" and self.transport is None:
                session = upstream_headers.get("session-id") or upstream_headers.get("thread-id") or uuid.uuid4().hex
                ws_metrics = metrics if metrics is not None else {}
                try:
                    response = await self.websockets.response(
                        BASE.replace("https:", "wss:") + path, (account_pk, observed, session),
                        upstream_headers, payload, ws_metrics,
                    )
                except GatewayError as exc:
                    if self.ctx.settings.gateway_transport != "auto" or payload.get("previous_response_id"):
                        raise
                    ws_metrics["ws_fallback_error"] = exc.code
                    response = None
                if response is None:
                    pass
                elif response.status_code == 401 and not attempt:
                    await response.aclose()
                    rejected_version = observed
                    continue
                elif (self.ctx.settings.gateway_transport == "websocket" or payload.get("previous_response_id")
                      or response.status_code not in (400, 403, 404, 405, 426, 501)):
                    return response
                else:
                    await response.aclose()
                    if metrics is not None:
                        metrics["ws_fallback_status"] = response.status_code
            try:
                # A standalone Request avoids sharing upstream cookies across bound accounts.
                request = httpx.Request(
                    "GET" if payload is None else "POST", BASE + path, headers=upstream_headers,
                    params={"client_version": version} if version is not None else None, json=payload,
                )
            except (ValueError, RecursionError):
                raise GatewayError(400, "invalid_request", "The request body cannot be encoded as JSON.") from None
            if metrics is not None:
                metrics["request_bytes"] = len(request.content)
            if (payload is not None and self.ctx.settings.gateway_request_compression
                    and len(request.content) >= self.ctx.settings.gateway_compression_min_bytes):
                compressed = await asyncio.to_thread(zstandard.ZstdCompressor(level=1).compress, request.content)
                if len(compressed) < len(request.content):
                    request.headers["content-encoding"] = "zstd"
                    request.headers["content-length"] = str(len(compressed))
                    request = httpx.Request(request.method, request.url, headers=request.headers, content=compressed)
            if metrics is not None:
                metrics["wire_bytes"] = len(request.content)
                trace_started = {}

                async def trace(name, info):
                    operation, _, state = name.rpartition(".")
                    if state == "started":
                        trace_started[operation] = time.monotonic()
                    elif state in ("complete", "failed") and operation in trace_started:
                        if operation.endswith(("connect_tcp", "start_tls")):
                            field = operation.rsplit(".", 1)[-1] + "_ms"
                            metrics[field] = metrics.get(field, 0) + round((time.monotonic() - trace_started.pop(operation)) * 1000, 2)

                request.extensions["trace"] = trace
            try:
                started = time.monotonic()
                response = await self.client.send(request, stream=True)
                if metrics is not None:
                    metrics["upstream_headers_ms"] = round((time.monotonic() - started) * 1000, 2)
                    metrics["upstream_protocol"] = response.http_version
                    metrics["auth_retries"] = attempt
            except httpx.TimeoutException:
                raise GatewayError(504, "upstream_timeout", "The Codex upstream timed out.") from None
            except (httpx.HTTPError, UnicodeError):
                raise GatewayError(502, "upstream_connection_error", "Could not reach the Codex upstream.") from None
            if response.status_code != 401 or attempt:
                return response
            await response.aclose()
            rejected_version = observed
        raise AssertionError("unreachable")

    async def _refresh_catalog(self, account_pk: int, version: str) -> dict:
        try:
            async with asyncio.timeout(30):
                response = await self.send(account_pk, "/models", version=version)
                try:
                    if response.status_code != 200:
                        raise upstream_status(response)
                    catalog = await bounded_json(response, MAX_CATALOG)
                finally:
                    await response.aclose()
            models = catalog.get("models")
            seen = set()
            if isinstance(models, list):
                for model in models:
                    slug = model.get("slug") if isinstance(model, dict) else None
                    if not isinstance(slug, str) or not storage.MODEL_ID.fullmatch(slug) or slug in seen:
                        raise GatewayError(502, "invalid_model_catalog", "The upstream model catalog is invalid.")
                    seen.add(slug)
            else:
                raise GatewayError(502, "invalid_model_catalog", "The upstream model catalog is invalid.")

            def save():
                with db.connection(self.ctx.settings.db_path) as conn:
                    storage.store_catalog(conn, account_pk, version, version == self.ctx.settings.gateway_client_version,
                                          catalog, self.ctx.now())

            await run_in_threadpool(save)
            return catalog
        except (TimeoutError, httpx.TimeoutException):
            raise GatewayError(504, "catalog_timeout", "The upstream model catalog timed out.") from None
        except httpx.HTTPError:
            raise GatewayError(502, "invalid_model_catalog", "Could not read the upstream model catalog.") from None

    async def catalog(self, account_pk: int, version: str, *, force: bool = False) -> dict:
        if not VERSION.fullmatch(version):
            raise GatewayError(400, "invalid_client_version", "client_version must have the form major.minor.patch.")

        def read():
            with db.connection(self.ctx.settings.db_path) as conn:
                return storage.get_catalog(conn, account_pk, version)

        cached = await run_in_threadpool(read)
        if cached and not force and self.ctx.now() - cached[1] < 3600:
            return cached[0]
        ident = (account_pk, version)
        task = self.catalog_tasks.get(ident)
        if task is None:
            task = asyncio.create_task(self._refresh_catalog(account_pk, version))
            self.catalog_tasks[ident] = task

            def remove(done):
                self.catalog_tasks.pop(ident, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(remove)
        try:
            return await asyncio.shield(task)
        except GatewayError:
            if cached and not force:
                return cached[0]
            raise

    async def test_model(self, account_pk: int, model: str, *, fast: bool, actor: str) -> None:
        def preflight():
            with db.connection(self.ctx.settings.db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM account_models WHERE account_pk = ? AND model_id = ?",
                    (account_pk, model),
                ).fetchone()
            if not row or not row["upstream_available"] or not row["selectable"] or (fast and row["verified_at"] is None):
                raise GatewayError(400, "invalid_model", "请先同步目录，Fast 测试之前需通过标准测试")

        def save(ok: bool):
            with db.connection(self.ctx.settings.db_path) as conn:
                storage.verify_model(conn, account_pk, model, fast, ok, actor, self.ctx.now())

        await run_in_threadpool(preflight)
        payload = {
            "model": model, "instructions": "Reply concisely.", "input": "Reply with OK.",
            "stream": True, "reasoning": {"effort": "low"},
        }
        if fast:
            payload["service_tier"] = "priority"
        payload, _ = adapt(payload, Key(0, "", account_pk, fast, {model: fast}))
        completed = None
        has_text = False
        ended_early = False
        try:
            async with asyncio.timeout(45):
                response = await self.send(account_pk, "/responses", payload=payload)
                try:
                    if response.status_code != 200:
                        raise upstream_status(response)
                    async for _, events in sse(response):
                        for event in events:
                            typ = event.get("type")
                            if (typ == "response.output_text.delta" and event.get("delta")
                                    or typ == "response.output_text.done" and event.get("text")):
                                has_text = True
                            if typ == "response.completed" and isinstance(event.get("response"), dict):
                                completed = event["response"]
                                break
                            if typ in ("response.failed", "response.incomplete", "error"):
                                ended_early = True
                                break
                        if completed is not None or ended_early:
                            break
                finally:
                    await response.aclose()
        except (TimeoutError, httpx.TimeoutException):
            raise GatewayError(504, "model_test_timeout", "模型测试超时") from None
        except httpx.HTTPError:
            raise GatewayError(502, "model_test_failed", "模型连接中断") from None
        if completed is None:
            await run_in_threadpool(save, False)
            raise GatewayError(502, "model_test_incomplete", "上游未完成测试请求")
        if not has_text:
            has_text = any(
                part.get("type") == "output_text" and part.get("text")
                for item in completed.get("output") or [] if isinstance(item, dict) and item.get("type") == "message"
                for part in item.get("content") or [] if isinstance(part, dict)
            )
        tier = completed.get("service_tier")
        ok = has_text and (not fast or tier in ("priority", "fast"))
        await run_in_threadpool(save, ok)
        if not has_text:
            raise GatewayError(502, "model_test_no_text", "上游已完成请求，但未返回测试文本")
        if fast and not ok:
            if tier == "default":
                raise GatewayError(409, "fast_downgraded", "Fast 测试已完成，上游实际使用标准档 (default)")
            raise GatewayError(409, "fast_unconfirmed", "Fast 测试已完成，但上游没有确认使用 Fast 档")


class Events:
    """Inspect bounded SSE records without altering the stream sent to the client."""

    def __init__(self, max_event: int = MAX_EVENT):
        self.line = bytearray()
        self.data = bytearray()
        self.discard_line = False
        self.discard_event = False
        self.max_event = max_event

    def feed(self, chunk: bytes):
        lines = chunk.split(b"\n")
        for index, piece in enumerate(lines):
            if len(self.line) + len(piece) > self.max_event:
                self.line.clear()
                self.data.clear()
                self.discard_line = True
                self.discard_event = True
            if not self.discard_line:
                self.line.extend(piece)
            if index == len(lines) - 1:
                return
            if self.discard_line:
                self.discard_line = False
                continue
            line = self.line.rstrip(b"\r")
            self.line.clear()
            if line.startswith(b"data:") and not self.discard_event:
                value = line[5:].lstrip(b" ")
                if len(self.data) + len(value) + 1 > self.max_event:
                    self.data.clear()
                    self.discard_event = True
                else:
                    self.data.extend(value + b"\n")
            elif not line and self.discard_event:
                self.discard_event = False
            elif not line and self.data:
                try:
                    event = json.loads(self.data)
                except (ValueError, RecursionError):
                    event = None
                self.data.clear()
                if isinstance(event, dict):
                    yield event


async def sse(response: httpx.Response, *, max_event: int = MAX_EVENT) -> AsyncIterator[tuple[bytes, list[dict]]]:
    parser = Events(max_event)
    async for chunk in response.aiter_bytes():
        yield chunk, list(parser.feed(chunk))
