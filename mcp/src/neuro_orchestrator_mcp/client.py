"""Thin async HTTP client for the neuro-orchestrator REST API.

The MCP server is a lightweight client of a *running* orchestrator (FastAPI, default :8080);
it does not embed the neuroimaging stack. Base URL comes from NEURO_ORCHESTRATOR_URL.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_URL = "http://localhost:8080"


class OrchestratorError(RuntimeError):
    """Raised when the orchestrator is unreachable or returns an error."""


class OrchestratorClient:
    def __init__(self, base_url: str | None = None, timeout: float = 120.0) -> None:
        self.base_url = (base_url or os.environ.get("NEURO_ORCHESTRATOR_URL", DEFAULT_URL)).rstrip("/")
        self.timeout = timeout

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise OrchestratorError(
                f"Cannot reach the neuro-orchestrator at {self.base_url}. Start it first "
                f"(`docker-compose up -d orchestrator`, or run its run_server.py with MOCK_MODE=1), "
                f"or set NEURO_ORCHESTRATOR_URL. Details: {e}"
            ) from e
        if r.status_code >= 400:
            try:
                detail: Any = r.json()
            except Exception:
                detail = r.text
            raise OrchestratorError(f"{method} {path} returned HTTP {r.status_code}: {detail}")
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, json: Any | None = None, **kwargs: Any) -> Any:
        return await self.request("POST", path, json=json, **kwargs)
