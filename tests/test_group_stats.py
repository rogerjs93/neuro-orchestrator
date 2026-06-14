import numpy as np
import pytest

from pipeline.group_stats import (
    benjamini_hochberg,
    cohens_d,
    compare_network_metrics,
    compare_fc_matrices,
    compare_fc_permutation,
    groups_from_participants,
)


def test_builtin_bh_matches_statsmodels():
    p = [0.001, 0.01, 0.02, 0.2, 0.8, 0.04, 0.5]
    from statsmodels.stats.multitest import multipletests
    _, q_sm, _, _ = multipletests(p, method="fdr_bh")
    assert np.allclose(benjamini_hochberg(p), q_sm, atol=1e-9)


def test_bh_bounds_and_monotonicity():
    p = np.array([0.001, 0.01, 0.02, 0.2, 0.8])
    q = benjamini_hochberg(p)
    assert np.all(q >= p - 1e-12)          # q never below p
    assert np.all(q <= 1.0)
    order = np.argsort(p)
    assert np.all(np.diff(q[order]) >= -1e-12)  # monotone non-decreasing in p order


def test_cohens_d_sign_and_magnitude():
    a = np.zeros(10) + 5.0 + np.array([0.1, -0.1] * 5)
    b = np.zeros(10) + 0.0 + np.array([0.1, -0.1] * 5)
    d = cohens_d(a, b)
    assert d > 5  # large positive effect


def test_network_metrics_detects_real_difference_only():
    rng = np.random.default_rng(0)
    groups = {"patient": [f"sub-p{i}" for i in range(15)],
              "control": [f"sub-c{i}" for i in range(15)]}
    metrics = {}
    for sid in groups["patient"]:
        metrics[sid] = {"global_efficiency": float(rng.normal(3.0, 1.0)),
                        "modularity_Q": float(rng.normal(0.0, 1.0))}
    for sid in groups["control"]:
        metrics[sid] = {"global_efficiency": float(rng.normal(0.0, 1.0)),
                        "modularity_Q": float(rng.normal(0.0, 1.0))}
    res = compare_network_metrics(metrics, groups, alpha=0.05)
    by = {r["metric"]: r for r in res["metrics"]}
    assert by["global_efficiency"]["significant"] is True   # real shift
    assert by["modularity_Q"]["significant"] is False        # null
    assert res["n_significant"] == 1


def test_network_metrics_requires_two_groups():
    with pytest.raises(ValueError):
        compare_network_metrics({}, {"only": ["sub-1"]})


def test_fc_matrices_finds_shifted_edges():
    rng = np.random.default_rng(1)
    n, k = 20, 12

    def rand_fc():
        x = rng.normal(size=(n, n))
        m = (x + x.T) / 2
        np.fill_diagonal(m, 1.0)
        return m

    groups = {"A": [f"a{i}" for i in range(k)], "B": [f"b{i}" for i in range(k)]}
    fc = {}
    for sid in groups["A"]:
        fc[sid] = rand_fc()
    for sid in groups["B"]:
        m = rand_fc()
        m[0, 1] = m[1, 0] = m[0, 1] + 6.0  # one strongly shifted edge
        fc[sid] = m
    res = compare_fc_matrices(fc, groups, alpha=0.05)
    assert res["n_edges"] == n * (n - 1) // 2
    assert res["n_significant"] >= 1
    top = res["top_edges"][0]
    assert {top["i"], top["j"]} == {0, 1}   # the planted edge is the strongest
    assert top["significant"] is True


def test_fc_permutation_fwe_finds_planted_edge():
    rng = np.random.default_rng(2)
    n, k = 16, 12

    def rand_fc():
        x = rng.normal(size=(n, n))
        m = (x + x.T) / 2
        np.fill_diagonal(m, 1.0)
        return m

    groups = {"A": [f"a{i}" for i in range(k)], "B": [f"b{i}" for i in range(k)]}
    fc = {}
    for sid in groups["A"]:
        fc[sid] = rand_fc()
    for sid in groups["B"]:
        m = rand_fc()
        m[0, 1] = m[1, 0] = m[0, 1] + 8.0  # strong planted difference
        fc[sid] = m
    res = compare_fc_permutation(fc, groups, alpha=0.05, n_perm=500)
    assert res["method"].startswith("permutation OLS")
    assert res["n_significant"] >= 1
    assert {res["top_edges"][0]["i"], res["top_edges"][0]["j"]} == {0, 1}


def test_fc_requires_two_per_group():
    fc = {"a0": np.eye(5), "b0": np.eye(5)}
    with pytest.raises(ValueError):
        compare_fc_matrices(fc, {"A": ["a0"], "B": ["b0"]})


def test_groups_from_participants(tmp_path):
    p = tmp_path / "participants.tsv"
    p.write_text("participant_id\tgroup\tage\n"
                 "sub-01\tpatient\t40\n"
                 "sub-02\tcontrol\t41\n"
                 "sub-03\tpatient\t39\n"
                 "sub-04\t\t50\n", encoding="utf-8")
    groups = groups_from_participants(tmp_path, "group")
    assert groups == {"patient": ["sub-01", "sub-03"], "control": ["sub-02"]}
