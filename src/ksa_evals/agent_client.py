"""Client for the agent under test: anything speaking the /api/chat contract.

    POST { tenantId, sessionId, message }  ->  { reply }
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import httpx


class AgentError(Exception):
    """The agent failed to produce a reply (HTTP error, timeout, empty body)."""


@dataclass
class AgentReply:
    text: str
    latency_s: float


class AgentClient(Protocol):
    async def send(self, tenant_id: str, session_id: str, message: str) -> AgentReply: ...


class HttpAgentClient:
    def __init__(self, url: str, timeout_s: float = 90.0):
        self.url = url
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def send(self, tenant_id: str, session_id: str, message: str) -> AgentReply:
        started = time.perf_counter()
        try:
            res = await self._client.post(self.url, json={"tenantId": tenant_id, "sessionId": session_id, "message": message})
        except httpx.HTTPError as err:
            raise AgentError(f"{type(err).__name__}: {err}") from err
        latency = time.perf_counter() - started
        if res.status_code != 200:
            raise AgentError(f"HTTP {res.status_code}: {res.text[:200]}")
        reply = (res.json().get("reply") or "").strip()
        if not reply:
            raise AgentError("Empty reply")
        return AgentReply(text=reply, latency_s=latency)

    async def aclose(self) -> None:
        await self._client.aclose()
