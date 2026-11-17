from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from codexctl.gateway.storage import GatewayError


@dataclass(eq=False)
class Connection:
    socket: object
    scope: tuple[int, int, str]
    used_at: float = field(default_factory=time.monotonic)
    busy: bool = True
    responses: list[str] = field(default_factory=list)


class WebSocketPool:
    """Lease each upstream socket to one turn within an account, credential and session."""

    def __init__(self, settings):
        self.settings = settings
        self.connections: list[Connection] = []
        self.closed = False
        self.sweeper: asyncio.Task | None = None

    def start(self):
        self.sweeper = asyncio.create_task(self._sweep())

    async def _sweep(self):
        while True:
            await asyncio.sleep(min(30, self.settings.gateway_ws_idle_seconds))
            await self.expire()

    async def expire(self):
        now = time.monotonic()
        expired = [entry for entry in self.connections if not entry.busy
                   and now - entry.used_at >= self.settings.gateway_ws_idle_seconds]
        for entry in expired:
            self.connections.remove(entry)
        await asyncio.gather(*(entry.socket.close() for entry in expired), return_exceptions=True)

    async def close(self):
        self.closed = True
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        entries, self.connections = self.connections, []
        await asyncio.gather(*(entry.socket.close() for entry in entries), return_exceptions=True)

    async def release(self, entry: Connection, reusable: bool):
        entry.busy = False
        entry.used_at = time.monotonic()
        idle = sum(not item.busy for item in self.connections)
        if self.closed or not reusable or idle > self.settings.gateway_ws_max_idle_connections:
            if entry in self.connections:
                self.connections.remove(entry)
            await entry.socket.close()

    async def response(self, url: str, scope: tuple[int, int, str], headers: dict,
                       payload: dict, metrics: dict) -> httpx.Response:
        await self.expire()
        previous = payload.get("previous_response_id")
        candidates = [entry for entry in self.connections if entry.scope == scope]
        if previous:
            candidates = [entry for entry in candidates if previous in entry.responses]
            if not candidates:
                raise GatewayError(409, "previous_response_unavailable",
                                   "The previous response is unavailable for this session. Send the full input context.")
        entry = next((entry for entry in reversed(candidates) if not entry.busy), None)
        if entry is None and previous:
            raise GatewayError(409, "session_busy", "The previous response is still in use. Retry after the active turn.")
        metrics["ws_reused"] = entry is not None
        if entry is None:
            started = time.monotonic()
            ws_headers = {**headers, "openai-beta": "responses_websockets=2026-02-06"}
            user_agent = ws_headers.pop("user-agent", None)
            try:
                socket = await connect(url, additional_headers=ws_headers, user_agent_header=user_agent,
                                       proxy=None, open_timeout=self.settings.http_timeout,
                                       close_timeout=3, max_size=16 * 1024 * 1024,
                                       ping_interval=20, ping_timeout=30)
            except InvalidStatus as exc:
                return httpx.Response(exc.response.status_code, headers=dict(exc.response.headers))
            except (OSError, TimeoutError, WebSocketException):
                raise GatewayError(502, "upstream_connection_error", "Could not open the upstream WebSocket.") from None
            entry = Connection(socket, scope)
            self.connections.append(entry)
            metrics["ws_connect_ms"] = round((time.monotonic() - started) * 1000, 2)
        entry.busy = True
        body = {**payload, "type": "response.create"}
        body.pop("stream", None)
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        metrics.update(upstream_protocol="websocket", wire_bytes=len(encoded.encode()))
        try:
            await entry.socket.send(encoded)
        except (OSError, WebSocketException):
            await self.release(entry, False)
            raise httpx.WriteError("Could not send the upstream WebSocket request.") from None
        except BaseException:
            await self.release(entry, False)
            raise
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=WebSocketStream(self, entry))


class WebSocketStream(httpx.AsyncByteStream):
    def __init__(self, pool: WebSocketPool, entry: Connection):
        self.pool = pool
        self.entry = entry
        self.complete = False
        self.closed = False

    async def __aiter__(self):
        try:
            while True:
                raw = await asyncio.wait_for(self.entry.socket.recv(), timeout=300)
                event = json.loads(raw)
                kind = event.get("type")
                if not isinstance(kind, str) or not kind.isascii() or not kind.replace(".", "").replace("_", "").isalnum():
                    raise ValueError("invalid event type")
                if kind == "response.completed":
                    self.complete = True
                    response_id = event.get("response", {}).get("id")
                    if isinstance(response_id, str):
                        self.entry.responses.append(response_id)
                        self.entry.responses = self.entry.responses[-64:]
                data = raw.encode() if isinstance(raw, str) else raw
                data = b"\ndata: ".join(data.splitlines())
                yield b"event: " + kind.encode() + b"\ndata: " + data + b"\n\n"
                if kind in ("response.completed", "response.failed", "response.incomplete", "error"):
                    return
        except TimeoutError:
            raise httpx.ReadTimeout("The upstream WebSocket timed out.") from None
        except (ConnectionClosed, WebSocketException, ValueError, TypeError, AttributeError):
            raise httpx.ReadError("The upstream WebSocket ended before completion.") from None

    async def aclose(self):
        if not self.closed:
            self.closed = True
            await self.pool.release(self.entry, self.complete)
