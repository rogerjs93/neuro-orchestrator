"""Tests for the neuro-orchestrator MCP tools.

Unit tests (schema/validation) run offline. Integration tests need a running orchestrator on
NEURO_ORCHESTRATOR_URL (default http://localhost:8080, e.g. MOCK_MODE=1 run_server.py) and skip
themselves when it is unreachable.
"""
import os

import httpx
import pytest

from neuro_orchestrator_mcp import server
from neuro_orchestrator_mcp.client import OrchestratorClient

pytestmark = pytest.mark.asyncio

BASE = os.environ.get("NEURO_ORCHESTRATOR_URL", "http://localhost:8080")


def _orchestrator_up() -> bool:
    try:
        httpx.get(f"{BASE}/api/subjects", timeout=3.0)
        return True
    except Exception:
        return False


UP = _orchestrator_up()
needs_server = pytest.mark.skipif(not UP, reason=f"orchestrator not reachable at {BASE}")


# ---- offline: tool registry + validation ----

async def test_expected_tools_registered():
    names = {t.name for t in await server.mcp.list_tools()}
    for expected in {"list_subjects", "run_subject", "run_stage", "generate_stl",
                     "run_group_stats", "review_mask_gate", "ingest_dicom"}:
        assert expected in names, f"missing tool {expected}"


async def test_run_stage_rejects_bad_stage():
    res = await server.run_stage("sub-01", "not-a-stage")
    assert "error" in res and "Valid stages" in res["error"]


async def test_generate_stl_rejects_bad_preset():
    res = await server.generate_stl("sub-01", preset="nope")
    assert "error" in res and "Valid presets" in res["error"]


async def test_group_stats_requires_groups():
    res = await server.run_group_stats(target="network")
    assert "error" in res


async def test_unreachable_orchestrator_is_friendly(monkeypatch):
    # Point the client at a dead port; the tool should return a readable error, not crash.
    monkeypatch.setattr(server, "_client", OrchestratorClient(base_url="http://127.0.0.1:59999"))
    res = await server.list_subjects()
    assert "error" in res and "Cannot reach" in res["error"]
    monkeypatch.setattr(server, "_client", None)


# ---- integration: needs a running orchestrator ----

@needs_server
async def test_list_subjects_live():
    server._client = None  # use env-configured URL
    snap = await server.list_subjects()
    assert "error" not in snap
    # the orchestrator ships sub-01 / sub-02
    text = str(snap)
    assert "sub-01" in text or "subjects" in snap


@needs_server
async def test_participant_columns_live():
    server._client = None
    res = await server.get_participant_columns()
    assert "error" not in res
