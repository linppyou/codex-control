from __future__ import annotations

import json
import logging
import time
import uuid

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .. import db
from . import storage, upstream
from .storage import GatewayError, Key, Usage
from .upstream import Gateway

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
    def __init__(self, gateway: Gateway, usage: Usage):
        self.gateway = gateway
        self.usage = usage
        self.started = time.monotonic()
        self.response: httpx.Response | None = None
        self.saved = False
        self.upstream_exhausted = False

    def elapsed(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def events(self, events: list[dict]) -> dict | None:
        result = None
        for event in events:
            typ = event.get("type")
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
                return storage.authorize(conn, request.headers.get("authorization", ""), ctx.now())
        return await run_in_threadpool(read)

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
        key = await authenticate(request)
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
        key = await authenticate(request)
        version = request.query_params.get("client_version", ctx.settings.gateway_client_version)
        if not upstream.VERSION.fullmatch(version):
            raise GatewayError(400, "invalid_client_version", "client_version must have the form major.minor.patch.")
        data = await gateway.catalog(key.account_pk, version) if key.models else {"models": []}
        # A canonical catalog refresh may have changed current availability.
        key = await authenticate(request)
        return JSONResponse(_native_catalog(data, key), headers=response_headers(request))

    @app.post("/responses/compact")
    async def retired_compact(request: Request):
        await authenticate(request)
        raise GatewayError(400, "legacy_compaction_unsupported", "Use a current Codex release with Responses compaction.")

    @app.post("/responses")
    async def responses(request: Request):
        key = await authenticate(request)
        headers = response_headers(request)
        try:
            payload = await _payload(request)
        except ClientDisconnect:
            return Response(status_code=499, headers=headers)
        payload, streaming = upstream.adapt(payload, key)
        forwarded = upstream.scope_session(key, payload, httpx.Headers(request.headers.items()), ctx.vault,
                                           streaming=streaming)
        exchange = Exchange(gateway, Usage(request.state.gateway_request_id, key, payload["model"], ctx.now(),
                                           payload.get("service_tier")))
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
                    response = await gateway.send(key.account_pk, "/responses", payload=payload, headers=forwarded)
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
