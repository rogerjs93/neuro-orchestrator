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


def test_group_stats_requires_two_groups():
    r = client.post("/api/group-stats", json={"target": "network", "groups": {"only": ["s1"]}})
    assert r.status_code == 400


def test_group_stats_network_endpoint_end_to_end():
    import json
    import numpy as np

    out = web_server.OUTPUT_DIR
    (out / "network").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(3)
    groups = {"patient": [], "control": []}
    for i in range(12):
        for grp, shift in (("patient", 3.0), ("control", 0.0)):
            sid = f"sub-{grp}{i}"
            p = out / "network" / f"{sid}_network_metrics.json"
            p.write_text(json.dumps({
                "global_efficiency": float(rng.normal(shift, 1.0)),
                "modularity_Q": float(rng.normal(0.0, 1.0)),
            }))
            web_server.manifest.register(subject=sid, role="network_metrics", path=p, stage="network")
            groups[grp].append(sid)

    r = client.post("/api/group-stats", json={"target": "network", "groups": groups, "alpha": 0.05})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "network_metrics"
    assert body["references"]                # citeable method recorded
    assert body["n_significant"] >= 1        # the planted global_efficiency difference
    assert body["saved_as"].startswith("group/")
