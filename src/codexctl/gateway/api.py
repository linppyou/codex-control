from __future__ import annotations

import json
import asyncio
import logging
import time
import uuid

import anyio
import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

import codexctl.db as db
import codexctl.gateway.storage as storage
import codexctl.gateway.upstream as upstream
from codexctl.gateway.storage import GatewayError, Key, Usage
from codexctl.gateway.upstream import Gateway

log = logging.getLogger(__name__)
FIRST_OUTPUT_EVENTS = {
    "response.output_text.delta", "response.refusal.delta", "response.function_call_arguments.delta",
    "response.custom_tool_call_input.delta",
}


def _error(error: GatewayError, headers: dict) -> JSONResponse:
    kind = {
        401: "authentication_error", 403: "permission_error", 429: "rate_limit_error",
    }.get(error.status, "invalid_request_error" if error.status < 500 else "server_error")
    headers = dict(headers)
    if error.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if error.retry_after:
        headers["Retry-After"] = error.retry_after
    return JSONResponse({"error": {"message": str(error), "type": kind, "code": error.code}},
                        status_code=error.status, headers=headers)


async def _payload(request: Request) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > upstream.MAX_BODY:
            raise GatewayError(413, "request_too_large", "The request body exceeds 16 MiB.")
        body += chunk
    try:
        decoded = json.loads(body)
        if isinstance(decoded, dict):
            return decoded
    except (ValueError, RecursionError):
        pass
    raise GatewayError(400, "invalid_request", "The request body must be a JSON object.")


class _Stream(StreamingResponse):
    def __init__(self, exchange: Exchange, headers: dict):
        super().__init__(exchange.stream(), media_type="text/event-stream", headers=headers)
        self.exchange = exchange

    async def __call__(self, scope, receive, send):
        # Handle disconnects while waiting on an idle upstream, even for ASGI servers
        # where StreamingResponse detects disconnects only when writing body chunks.
        try:
            async with anyio.create_task_group() as task_group:
                async def disconnect():
                    await self.listen_for_disconnect(receive)
                    task_group.cancel_scope.cancel()

                task_group.start_soon(disconnect)
                try:
                    await self.stream_response(send)
                except OSError:
                    pass
                finally:
                    task_group.cancel_scope.cancel()
        finally:
            if self.exchange.usage.status == "incomplete" and not self.exchange.upstream_exhausted:
                self.exchange.usage.status = "cancelled"
            await self.exchange.finish()


class Exchange:
    def __init__(self, gateway: Gateway, usage: Usage, *, started: float | None = None, metrics: dict | None = None):
        self.gateway = gateway
        self.usage = usage
        self.started = time.monotonic() if started is None else started
        self.metrics = {} if metrics is None else metrics
        self.response: httpx.Response | None = None
        self.saved = False
        self.upstream_exhausted = False

    def elapsed(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def events(self, events: list[dict]) -> dict | None:
        result = None
        for event in events:
            typ = event.get("type")
            if typ == "response.output_text.delta" and event.get("delta"):
                self.metrics.setdefault("first_text_ms", self.elapsed())
                self.metrics["last_text_ms"] = self.elapsed()
            if isinstance(typ, str) and typ in FIRST_OUTPUT_EVENTS and self.usage.first_output_ms is None:
                self.usage.first_output_ms = self.elapsed()
            if self.usage.status != "incomplete" or not isinstance(typ, str) or typ not in upstream.TERMINALS:
                continue
            terminal = event.get("response")
            if typ == "response.completed" and isinstance(terminal, dict):
                self.usage.status = "completed"
                self.usage.result = {key: terminal.get(key) for key in ("usage", "service_tier")}
                result = terminal
            else:
                self.usage.status = "upstream_failed"
        return result

    async def stream(self):
        try:
            async for chunk, events in upstream.sse(self.response):
                self.metrics.setdefault("first_byte_ms", self.elapsed())
                self.events(events)
                yield chunk
            self.upstream_exhausted = True
        except httpx.HTTPError:
            if self.usage.status == "incomplete":
                self.usage.status = "stream_interrupted"
        except anyio.get_cancelled_exc_class():
            if self.usage.status == "incomplete":
                self.usage.status = "cancelled"
            raise

    async def json_result(self) -> dict:
        async for _, events in upstream.sse(self.response):
            self.metrics.setdefault("first_byte_ms", self.elapsed())
            result = self.events(events)
            if result is not None:
                return result
            if self.usage.status == "upstream_failed":
                break
        raise GatewayError(502, "incomplete_response", "The Codex upstream did not complete the response.")

    async def finish(self):
        if self.saved:
            return
        self.saved = True
        self.usage.elapsed_ms = self.elapsed()

        def save():
            with db.connection(self.gateway.ctx.settings.db_path) as conn:
                self.usage.save(conn)

        with anyio.CancelScope(shield=True):
            try:
                if self.response is not None:
                    await self.response.aclose()
            finally:
                try:
                    await run_in_threadpool(save)
                except Exception:
                    log.exception("Could not store gateway usage for %s", self.usage.request_id)
                log.info("gateway_metrics %s", json.dumps({
                    "request_id": self.usage.request_id, "account_pk": self.usage.key.account_pk,
                    "model": self.usage.model, "status": self.usage.status,
                    "total_ms": self.usage.elapsed_ms, **self.metrics,
                }, separators=(",", ":")))


def _native_catalog(catalog: dict, key: Key) -> dict:
    models = []
    for original in catalog["models"]:
        slug = original["slug"]
        if slug not in key.models:
            continue
        model = original.copy()
        if not (key.allow_fast and key.models[slug]):
            model.update(service_tiers=[], additional_speed_tiers=[], default_service_tier=None)
        else:
            # A successful priority probe does not grant other premium tiers.
            model["service_tiers"] = [tier for tier in (model.get("service_tiers") or [])
                                      if isinstance(tier, dict) and tier.get("id") in ("priority", "fast")]
            model["additional_speed_tiers"] = [tier for tier in (model.get("additional_speed_tiers") or [])
                                               if tier in ("fast", "priority")]
            if model.get("default_service_tier") not in (None, "auto", "default", "priority", "fast"):
                model["default_service_tier"] = None
        models.append(model)
    return {"models": models}


def _compatible_model(model_id: str, upstream: dict) -> dict:
    entry = {"id": model_id, "object": "model", "created": 0, "owned_by": "openai"}
    name = upstream.get("display_name")
    if isinstance(name, str) and name:
        entry["display_name"] = name
    for upstream_field, field in (
        ("context_window", "default_context_window"), ("max_context_window", "max_context_window"),
        ("max_output_tokens", "max_output_tokens"),
    ):
        value = upstream.get(upstream_field)
        if type(value) is int and value > 0:
            entry[field] = value
    context = entry.get("max_context_window") or entry.get("default_context_window")
    if context:
        entry["context_window"] = context
    return entry


def create_api(gateway: Gateway) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, redirect_slashes=False)
    ctx = gateway.ctx

    async def authenticate(request: Request) -> Key:
        def read():
            with db.connection(ctx.settings.db_path) as conn:
                conn.execute("BEGIN")
                return storage.authorize(conn, request.headers.get("authorization", ""))
        return await run_in_threadpool(read)

    async def check_assignment(request: Request, account_pk: int) -> Key:
        key = await authenticate(request)
        if key.account_pk != account_pk:
            raise GatewayError(409, "assignment_changed", "The assigned Codex account changed. Retry the request.")
        return key

    async def with_models(request: Request) -> Key:
        key = await authenticate(request)
        if key.models:
            return key
        await gateway.catalog(key.account_pk, ctx.settings.gateway_client_version)
        return await check_assignment(request, key.account_pk)

    def response_headers(request: Request) -> dict:
        if not hasattr(request.state, "gateway_request_id"):
            request.state.gateway_request_id = uuid.uuid4().hex
        return {"X-Request-ID": request.state.gateway_request_id, "Cache-Control": "no-store"}

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, exc: GatewayError):
        return _error(exc, response_headers(request))

    @app.get("/")
    @app.get("/models")
    async def models(request: Request):
        key = await with_models(request)
        def read_metadata():
            with db.connection(ctx.settings.db_path) as conn:
                cached = storage.get_catalog(conn, key.account_pk, ctx.settings.gateway_client_version)
            return {item["slug"]: item for item in cached[0]["models"] if item["slug"] in key.models} if cached else {}

        metadata = await run_in_threadpool(read_metadata) if key.models else {}
        return JSONResponse({"object": "list", "data": [
            _compatible_model(model, metadata.get(model, {})) for model in key.models
        ]}, headers=response_headers(request))

    @app.get("/codex/models")
    async def catalog(request: Request):
        key = await with_models(request)
        version = request.query_params.get("client_version", ctx.settings.gateway_client_version)
        if not upstream.VERSION.fullmatch(version):
            raise GatewayError(400, "invalid_client_version", "client_version must have the form major.minor.patch.")
        data = await gateway.catalog(key.account_pk, version) if key.models else {"models": []}
        # A canonical catalog refresh may have changed current availability.
        key = await check_assignment(request, key.account_pk)
        return JSONResponse(_native_catalog(data, key), headers=response_headers(request))

    @app.post("/responses/compact")
    async def retired_compact(request: Request):
        await authenticate(request)
        raise GatewayError(400, "legacy_compaction_unsupported", "Use a current Codex release with Responses compaction.")

    @app.websocket("/responses")
    async def websocket_responses(socket: WebSocket):
        if ctx.settings.gateway_transport == "http":
            await socket.close(code=1008)
            return
        try:
            await with_models(socket)
        except GatewayError:
            await socket.close(code=1008)
            return
        await socket.accept()
        incoming = httpx.Headers(socket.headers.items())
        if "session-id" not in incoming and "session_id" not in incoming:
            incoming["session-id"] = uuid.uuid4().hex
        send_lock = asyncio.Lock()
        active: asyncio.Task | None = None

        async def send_event(event: dict):
            async with send_lock:
                await socket.send_json(event)

        async def send_error(error: GatewayError):
            await send_event({"type": "error", "status": error.status,
                              "error": {"code": error.code, "message": str(error)}})

        async def turn(message: dict):
            exchange = None
            started = time.monotonic()
            try:
                key = await with_models(socket)
                authenticated = time.monotonic()
                payload = {name: value for name, value in message.items() if name != "type"}
                if payload.get("stream_id") is not None:
                    raise GatewayError(400, "unsupported_stream_id", "Use a separate connection for each parallel conversation.")
                payload, _ = upstream.adapt(payload, key, allow_previous=True)
                headers = upstream.scope_session(key, payload, incoming, ctx.vault, streaming=True)
                metrics = {"downstream_protocol": "websocket",
                           "auth_ms": round((authenticated - started) * 1000, 2)}
                exchange = Exchange(gateway, Usage(uuid.uuid4().hex, key, payload["model"], ctx.now(),
                                                  payload.get("service_tier")), started=started, metrics=metrics)
                response = await gateway.send(key.account_pk, "/responses", payload=payload, headers=headers, metrics=metrics)
                exchange.response = response
                if not 200 <= response.status_code < 300:
                    raise upstream.upstream_status(response)
                async for _, events in upstream.sse(response, max_event=upstream.MAX_BODY):
                    metrics.setdefault("first_byte_ms", exchange.elapsed())
                    exchange.events(events)
                    for event in events:
                        await send_event(event)
                exchange.upstream_exhausted = True
            except GatewayError as exc:
                if exchange:
                    exchange.usage.status = exc.code
                await send_error(exc)
            except httpx.HTTPError:
                if exchange:
                    exchange.usage.status = "stream_interrupted"
                await send_error(GatewayError(502, "stream_interrupted", "The upstream stream was interrupted."))
            except asyncio.CancelledError:
                if exchange and exchange.usage.status == "incomplete":
                    exchange.usage.status = "cancelled"
                raise
            except (WebSocketDisconnect, OSError, RuntimeError):
                if exchange and exchange.usage.status == "incomplete":
                    exchange.usage.status = "cancelled"
            finally:
                if exchange:
                    await exchange.finish()

        try:
            while True:
                raw = await socket.receive_text()
                if len(raw.encode()) > upstream.MAX_BODY:
                    await send_error(GatewayError(413, "request_too_large", "The request body exceeds 16 MiB."))
                    continue
                try:
                    message = json.loads(raw)
                except (ValueError, RecursionError):
                    message = None
                if not isinstance(message, dict):
                    await send_error(GatewayError(400, "invalid_request", "Send a JSON object."))
                elif message.get("type") == "response.cancel":
                    if active and not active.done():
                        active.cancel()
                        await asyncio.gather(active, return_exceptions=True)
                elif message.get("type") != "response.create":
                    await send_error(GatewayError(400, "invalid_request", "Expected response.create or response.cancel."))
                elif active and not active.done():
                    await send_error(GatewayError(409, "session_busy", "Wait for the active response to finish."))
                else:
                    active = asyncio.create_task(turn(message))
        except (WebSocketDisconnect, OSError, RuntimeError):
            pass
        finally:
            if active:
                if not active.done():
                    active.cancel()
                await asyncio.gather(active, return_exceptions=True)

    @app.post("/responses")
    async def responses(request: Request):
        started = time.monotonic()
        key = await with_models(request)
        authenticated = time.monotonic()
        headers = response_headers(request)
        try:
            payload = await _payload(request)
        except ClientDisconnect:
            return Response(status_code=499, headers=headers)
        payload, streaming = upstream.adapt(payload, key, allow_previous=ctx.settings.gateway_transport != "http")
        forwarded = upstream.scope_session(key, payload, httpx.Headers(request.headers.items()), ctx.vault,
                                           streaming=streaming)
        metrics = {"auth_ms": round((authenticated - started) * 1000, 2),
                   "request_prepare_ms": round((time.monotonic() - authenticated) * 1000, 2)}
        exchange = Exchange(gateway, Usage(request.state.gateway_request_id, key, payload["model"], ctx.now(),
                                           payload.get("service_tier")), started=started, metrics=metrics)
        transferred = False
        try:
            result = None

            # A POST can be abandoned while upstream is still computing the response headers.
            async with anyio.create_task_group() as task_group:
                async def watch_disconnect():
                    while (await request.receive())["type"] != "http.disconnect":
                        pass
                    task_group.cancel_scope.cancel()

                task_group.start_soon(watch_disconnect)
                try:
                    response = await gateway.send(key.account_pk, "/responses", payload=payload,
                                                  headers=forwarded, metrics=metrics)
                    exchange.response = response
                    if not 200 <= response.status_code < 300:
                        result = upstream.upstream_status(response)
                    elif response.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in (
                        "", "text/event-stream",
                    ):
                        result = GatewayError(502, "invalid_upstream_response", "The upstream did not return SSE.")
                    elif streaming:
                        headers.update(upstream.response_headers(response, key, ctx.vault))
                        result = _Stream(exchange, dict(headers, **{"X-Accel-Buffering": "no"}))
                    else:
                        try:
                            result = JSONResponse(await exchange.json_result(), headers=headers)
                        except GatewayError as exc:
                            result = exc
                        except httpx.TimeoutException:
                            result = GatewayError(504, "upstream_timeout", "The Codex upstream timed out.")
                        except httpx.HTTPError:
                            result = GatewayError(502, "upstream_connection_error", "Could not read the upstream response.")
                except GatewayError as exc:
                    result = exc
                finally:
                    task_group.cancel_scope.cancel()
            if isinstance(result, GatewayError):
                exchange.usage.status = result.code
                raise result
            if result is None:
                exchange.usage.status = "cancelled"
                return Response(status_code=499, headers=headers)
            transferred = isinstance(result, _Stream)
            return result
        finally:
            if not transferred:
                await exchange.finish()

    return app
