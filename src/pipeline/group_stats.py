"""
Group-level statistics — the hypothesis-testing core.

Compares two groups of subjects on the per-subject artifacts the pipeline already
produces, resolved by role:
  - network metrics (scalars per subject) -> per-metric two-group test, FDR-corrected
  - functional connectivity matrices      -> edge-wise test, either a permutation
    FWE test (rigorous, default) or a mass-univariate FDR screen (fast)

It deliberately uses established, peer-reviewed, open-source tools rather than a
hand-rolled or proprietary (MATLAB) stack, so results are valid and citeable:
  - scipy.stats           — Welch t-test, Mann-Whitney U
  - statsmodels           — Benjamini-Hochberg FDR (multipletests)
  - Nilearn permuted_ols  — permutation inference with FWE max-statistic correction

Research-use note: standard exploratory group statistics, not a clinical
diagnostic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import numpy as np

# Method citations surfaced in result payloads for reporting.
REF_FDR = "Benjamini & Hochberg (1995), J. R. Statist. Soc. B 57:289-300."
REF_FWE = "Winkler et al. (2014), Permutation inference for the GLM, NeuroImage 92:381-397."
REF_NILEARN = "Abraham et al. (2014), Nilearn, Front. Neuroinform. 8:14."


def benjamini_hochberg(pvals: Sequence[float]) -> "np.ndarray":
    """Benjamini-Hochberg FDR q-values (numpy fallback; matches statsmodels fdr_bh)."""
    p = np.asarray(pvals, dtype=float)
    n = p.size
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * n / np.arange(1, n + 1)
    q_sorted = np.minimum.accumulate(ranked[::-1])[::-1]
    q = np.empty_like(q_sorted)
    q[order] = np.clip(q_sorted, 0.0, 1.0)
    return q


def _fdr(pvals: Sequence[float], alpha: float = 0.05) -> tuple["np.ndarray", str]:
    """FDR-correct p-values, preferring peer-reviewed statsmodels, with a numpy fallback."""
    p = np.asarray(pvals, dtype=float)
    if p.size == 0:
        return p, "none"
    try:
        from statsmodels.stats.multitest import multipletests
        _, q, _, _ = multipletests(p, alpha=alpha, method="fdr_bh")
        return q, "Benjamini-Hochberg FDR (statsmodels.multipletests)"
    except Exception:
        return benjamini_hochberg(p), "Benjamini-Hochberg FDR (builtin)"


def cohens_d(a: Sequence[float], b: Sequence[float]) -> float:
    """Cohen's d with pooled standard deviation (a minus b)."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    na, nb = a.size, b.size
    if na < 2 or nb < 2:
        return float("nan")
    va, vb = a.var(ddof=1), b.var(ddof=1)
    pooled = ((na - 1) * va + (nb - 1) * vb) / (na + nb - 2)
    if pooled <= 0:
        return 0.0
    return float((a.mean() - b.mean()) / np.sqrt(pooled))


def _two_labels(groups: Mapping[str, Sequence[str]]) -> tuple[str, str]:
    labels = list(groups.keys())
    if len(labels) != 2:
        raise ValueError("Group comparison currently supports exactly two groups")
    return labels[0], labels[1]


def _is_num(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and np.isfinite(v)


def _covariate_names(covariates, hint):
    if hint:
        return list(hint)
    if not covariates:
        return []
    names = set()
    for cv in covariates.values():
        for k, v in cv.items():
            if _is_num(v):
                names.add(k)
    return sorted(names)


def _has_all_covariates(sid, covariates, names):
    cv = (covariates or {}).get(sid, {})
    return all(_is_num(cv.get(nm)) for nm in names)


def _filter_groups_for_covariates(groups, covariates, names):
    """Keep only subjects that have every required covariate; report exclusions."""
    if not names:
        return groups, []
    filtered, excluded = {}, []
    for label, sids in groups.items():
        kept = []
        for sid in sids:
            if _has_all_covariates(sid, covariates, names):
                kept.append(sid)
            else:
                excluded.append(sid)
        filtered[label] = kept
    return filtered, excluded


def _covariate_rows(subjects, covariates, names):
    return np.asarray(
        [[float(covariates[sid][nm]) for nm in names] for sid in subjects], dtype=float
    ) if names else None


def compare_network_metrics(
    metrics_by_subject: Mapping[str, Mapping[str, Any]],
    groups: Mapping[str, Sequence[str]],
    alpha: float = 0.05,
    covariates: Mapping[str, Mapping[str, Any]] | None = None,
    covariate_names: Sequence[str] | None = None,
) -> Dict[str, Any]:
    """Two-group comparison of each scalar network metric, FDR-corrected across metrics.

    With covariates, each metric is tested via OLS (group effect adjusted for the
    covariates); otherwise a Welch t-test is used.
    """
    cov_names = _covariate_names(covariates, covariate_names)
    if cov_names:
        return _compare_network_glm(metrics_by_subject, groups, alpha, covariates, cov_names)

    from scipy.stats import ttest_ind, mannwhitneyu

    a_label, b_label = _two_labels(groups)

    def values(group: str, metric: str) -> List[float]:
        out: List[float] = []
        for sid in groups[group]:
            v = metrics_by_subject.get(sid, {}).get(metric)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            if np.isfinite(v):
                out.append(float(v))
        return out

    metric_keys = set()
    for m in metrics_by_subject.values():
        for k, v in m.items():
            if not isinstance(v, bool) and isinstance(v, (int, float)):
                metric_keys.add(k)

    rows: List[Dict[str, Any]] = []
    pvals: List[float] = []
    for metric in sorted(metric_keys):
        a, b = values(a_label, metric), values(b_label, metric)
        if len(a) < 2 or len(b) < 2:
            continue
        t, p = ttest_ind(a, b, equal_var=False)
        try:
            _, p_mw = mannwhitneyu(a, b, alternative="two-sided")
        except ValueError:
            p_mw = float("nan")
        rows.append({
            "metric": metric,
            f"n_{a_label}": len(a), f"n_{b_label}": len(b),
            f"mean_{a_label}": round(float(np.mean(a)), 6),
            f"mean_{b_label}": round(float(np.mean(b)), 6),
            "cohens_d": round(cohens_d(a, b), 4),
            "t": round(float(t), 4),
            "p": float(p),
            "p_mannwhitney": float(p_mw),
        })
        pvals.append(float(p))

    correction = "none"
    if pvals:
        q, correction = _fdr(pvals, alpha)
        for row, qi in zip(rows, q):
            row["p_fdr"] = float(qi)
            row["significant"] = bool(qi < alpha)

    return {
        "kind": "network_metrics",
        "method": "Welch t-test (scipy) per metric",
        "correction": correction,
        "references": [REF_FDR],
        "comparison": f"{a_label} vs {b_label}",
        "groups": {a_label: len(groups[a_label]), b_label: len(groups[b_label])},
        "alpha": alpha,
        "n_metrics": len(rows),
        "n_significant": sum(1 for r in rows if r.get("significant")),
        "metrics": rows,
    }


def _compare_network_glm(metrics_by_subject, groups, alpha, covariates, cov_names):
    """Per-metric OLS: test the group effect adjusting for covariates."""
    import statsmodels.api as sm

    a_label, b_label = _two_labels(groups)
    fgroups, excluded = _filter_groups_for_covariates(groups, covariates, cov_names)
    members = [(sid, 1.0) for sid in fgroups[a_label]] + [(sid, 0.0) for sid in fgroups[b_label]]

    metric_keys = set()
    for m in metrics_by_subject.values():
        for k, v in m.items():
            if _is_num(v):
                metric_keys.add(k)

    rows, pvals = [], []
    min_n = len(cov_names) + 3  # intercept + group + covariates + slack
    for metric in sorted(metric_keys):
        sids, y, g = [], [], []
        for sid, flag in members:
            v = metrics_by_subject.get(sid, {}).get(metric)
            if _is_num(v):
                sids.append(sid); y.append(float(v)); g.append(flag)
        n_a = int(sum(g)); n_b = len(g) - n_a
        if len(sids) < min_n or n_a < 2 or n_b < 2:
            continue
        cov = _covariate_rows(sids, covariates, cov_names)
        X = np.column_stack([np.ones(len(sids)), np.asarray(g, float), cov])
        try:
            model = sm.OLS(np.asarray(y, float), X).fit()
        except Exception:
            continue
        rows.append({
            "metric": metric, f"n_{a_label}": n_a, f"n_{b_label}": n_b,
            "beta_group": round(float(model.params[1]), 6),
            "t": round(float(model.tvalues[1]), 4), "p": float(model.pvalues[1]),
        })
        pvals.append(float(model.pvalues[1]))

    correction = "none"
    if pvals:
        q, correction = _fdr(pvals, alpha)
        for row, qi in zip(rows, q):
            row["p_fdr"] = float(qi)
            row["significant"] = bool(qi < alpha)

    return {
        "kind": "network_metrics",
        "method": f"OLS group effect adjusted for covariates ({', '.join(cov_names)})",
        "correction": correction,
        "references": [REF_FDR],
        "comparison": f"{a_label} vs {b_label}",
        "groups": {a_label: len(fgroups[a_label]), b_label: len(fgroups[b_label])},
        "covariates": list(cov_names),
        "excluded_subjects": excluded,
        "alpha": alpha,
        "n_metrics": len(rows),
        "n_significant": sum(1 for r in rows if r.get("significant")),
        "metrics": rows,
    }


def _stack_fc(fc_by_subject, groups, a_label, b_label):
    """Return (subject_ids, group_indicator, edge_matrix, n_nodes, triu_indices)."""
    subjects: List[str] = []
    indicator: List[float] = []
    mats: List["np.ndarray"] = []
    for label, flag in ((a_label, 1.0), (b_label, 0.0)):
        for sid in groups[label]:
            if sid in fc_by_subject:
                subjects.append(sid)
                indicator.append(flag)
                mats.append(np.asarray(fc_by_subject[sid], dtype=float))
    n_a = int(sum(indicator))
    n_b = len(indicator) - n_a
    if n_a < 2 or n_b < 2:
        raise ValueError("Need at least two FC matrices per group")
    shape = mats[0].shape
    if shape[0] != shape[1] or any(m.shape != shape for m in mats):
        raise ValueError("FC matrices must be square and the same shape across subjects")
    n = shape[0]
    iu = np.triu_indices(n, k=1)
    edges = np.vstack([m[iu[0], iu[1]] for m in mats])  # (nSubj, nEdges)
    return subjects, np.asarray(indicator, dtype=float), edges, n, iu


def compare_fc_matrices(
    fc_by_subject: Mapping[str, "np.ndarray"],
    groups: Mapping[str, Sequence[str]],
    alpha: float = 0.05,
    top: int = 25,
) -> Dict[str, Any]:
    """Fast screen: mass-univariate edge-wise Welch t-test with BH-FDR."""
    from scipy.stats import ttest_ind

    a_label, b_label = _two_labels(groups)
    subjects, indicator, edges, n, iu = _stack_fc(fc_by_subject, groups, a_label, b_label)
    av, bv = edges[indicator == 1.0], edges[indicator == 0.0]

    t, p = ttest_ind(av, bv, axis=0, equal_var=False)
    finite = np.isfinite(p)
    q = np.full(p.shape, np.nan)
    correction = "none"
    if finite.any():
        q[finite], correction = _fdr(p[finite], alpha)
    sig = finite & (q < alpha)

    idx = np.where(finite)[0]
    order = idx[np.argsort(-np.abs(t[idx]))][:top] if idx.size else np.array([], dtype=int)
    top_edges = [{
        "i": int(iu[0][e]), "j": int(iu[1][e]),
        "t": round(float(t[e]), 4), "p": float(p[e]),
        "p_fdr": float(q[e]), "significant": bool(sig[e]),
    } for e in order]

    return {
        "kind": "fc_matrix",
        "method": "mass-univariate Welch t-test (scipy) — screening",
        "correction": correction,
        "references": [REF_FDR],
        "comparison": f"{a_label} vs {b_label}",
        "groups": {a_label: int((indicator == 1.0).sum()), b_label: int((indicator == 0.0).sum())},
        "alpha": alpha,
        "n_edges": int(finite.sum()),
        "n_significant": int(sig.sum()),
        "top_edges": top_edges,
    }


def compare_fc_permutation(
    fc_by_subject: Mapping[str, "np.ndarray"],
    groups: Mapping[str, Sequence[str]],
    alpha: float = 0.05,
    n_perm: int = 5000,
    top: int = 25,
    random_state: int = 0,
    covariates: Mapping[str, Mapping[str, Any]] | None = None,
    covariate_names: Sequence[str] | None = None,
) -> Dict[str, Any]:
    """Rigorous edge-wise group test via Nilearn permutation OLS (FWE max-stat).

    Covariates are passed as confounding_vars so the group effect is tested while
    adjusting for them.
    """
    from nilearn.mass_univariate import permuted_ols

    a_label, b_label = _two_labels(groups)
    cov_names = _covariate_names(covariates, covariate_names)
    work_groups, excluded = (
        _filter_groups_for_covariates(groups, covariates, cov_names) if cov_names else (groups, [])
    )
    subjects, indicator, edges, n, iu = _stack_fc(fc_by_subject, work_groups, a_label, b_label)
    confounds = _covariate_rows(subjects, covariates, cov_names) if cov_names else None

    out = permuted_ols(
        indicator.reshape(-1, 1), edges, confounding_vars=confounds,
        n_perm=n_perm, two_sided_test=True, random_state=random_state,
        output_type="dict", verbose=0,
    )
    t = np.asarray(out["t"]).ravel()
    logp = np.asarray(out["logp_max_t"]).ravel()          # -log10 FWE-corrected p
    corrected_p = np.power(10.0, -logp)
    sig = corrected_p < alpha

    order = np.argsort(-np.abs(t))[:top]
    top_edges = [{
        "i": int(iu[0][e]), "j": int(iu[1][e]),
        "t": round(float(t[e]), 4),
        "p_fwe": float(corrected_p[e]),
        "significant": bool(sig[e]),
    } for e in order]

    return {
        "kind": "fc_matrix",
        "method": "permutation OLS, FWE max-statistic (Nilearn permuted_ols)",
        "correction": "FWE (max-stat permutation)",
        "references": [REF_FWE, REF_NILEARN],
        "comparison": f"{a_label} vs {b_label}",
        "groups": {a_label: int((indicator == 1.0).sum()), b_label: int((indicator == 0.0).sum())},
        "covariates": list(cov_names),
        "excluded_subjects": excluded,
        "alpha": alpha,
        "n_perm": int(n_perm),
        "n_edges": int(t.size),
        "n_significant": int(sig.sum()),
        "top_edges": top_edges,
    }


def groups_from_participants(data_dir: Path, column: str) -> Dict[str, List[str]]:
    """Read BIDS participants.tsv and group subject ids by the given phenotype column."""
    import csv

    path = Path(data_dir) / "participants.tsv"
    groups: Dict[str, List[str]] = {}
    if not path.is_file():
        return groups
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            sid = (row.get("participant_id") or row.get("participant") or "").strip()
            value = (row.get(column) or "").strip()
            if not sid or value in ("", "n/a", "N/A"):
                continue
            groups.setdefault(value, []).append(sid)
    return groups


def covariates_from_participants(data_dir: Path, columns: Sequence[str]) -> Dict[str, Dict[str, float]]:
    """Read BIDS participants.tsv numeric covariate columns into {subject: {col: value}}."""
    import csv

    path = Path(data_dir) / "participants.tsv"
    out: Dict[str, Dict[str, float]] = {}
    if not path.is_file() or not columns:
        return out
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            sid = (row.get("participant_id") or row.get("participant") or "").strip()
            if not sid:
                continue
            vals: Dict[str, float] = {}
            for col in columns:
                raw = (row.get(col) or "").strip()
                try:
                    vals[col] = float(raw)
                except (TypeError, ValueError):
                    continue
            if vals:
                out[sid] = vals
    return out
