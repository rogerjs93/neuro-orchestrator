"""API smoke tests via FastAPI TestClient.

conftest sets DATA_DIR/OUTPUT_DIR to temp dirs + MOCK_MODE before this imports
web_server, so the app boots against throwaway storage with no subjects.
"""
import pytest

import web_server
from fastapi.testclient import TestClient

client = TestClient(web_server.app)


def test_subjects_snapshot_has_gates_block():
    r = client.get("/api/subjects")
    assert r.status_code == 200
    body = r.json()
    assert "gates" in body
    for key in ("config", "pending", "audit"):
        assert key in body["gates"]


def test_gate_config_get_has_mask():
    r = client.get("/api/gate-config")
    assert r.status_code == 200
    assert "mask" in r.json()["gate_config"]


def test_gate_config_set_valid():
    r = client.post("/api/gate-config", json={"stage": "mask", "mode": "gated", "trigger": "on_flag"})
    assert r.status_code == 200
    cfg = r.json()["gate_config"]["mask"]
    assert cfg == {"mode": "gated", "trigger": "on_flag"}


@pytest.mark.parametrize("payload", [
    {"stage": "not_a_stage"},
    {"stage": "mask", "mode": "bogus"},
    {"stage": "mask", "mode": "gated", "trigger": "bogus"},
])
def test_gate_config_rejects_bad_input(payload):
    assert client.post("/api/gate-config", json=payload).status_code == 400


def test_rerun_unknown_subject_404():
    assert client.post("/api/rerun/sub-nope/fastsurfer", json={}).status_code == 404


def test_gate_decision_unknown_subject_404():
    assert client.post("/api/gate/sub-nope/mask", json={"decision": "approve"}).status_code == 404
