"""
Graph network analysis using Brain Connectivity Toolbox + NetworkX.
Called as: python -m pipeline.tasks.network --subject sub-001 ...
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Any

import numpy as np


def run(
    subject_id: str,
    connectivity_dir: Path,
    tractography_dir: Path,
    output_dir: Path,
) -> None:
    print(f"[network] Running graph analysis for {subject_id}")
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Load FC matrix ──────────────────────────────────────────────────────
    fc_file = connectivity_dir / f"{subject_id}_fc_matrix.npy"
    if not fc_file.exists():
        raise FileNotFoundError(f"FC matrix not found: {fc_file}")

    fc = np.load(fc_file)
    n = fc.shape[0]
    print(f"[network] Loaded FC matrix: {n}×{n}")

    # Threshold: keep top 20% of connections (proportional thresholding)
    threshold = np.percentile(np.abs(fc), 80)
    W = np.where(np.abs(fc) >= threshold, fc, 0)
    W[W < 0] = 0   # BCT works with non-negative weights

    metrics: Dict[str, Any] = {"subject": subject_id, "n_parcels": n}

    # ── Brain Connectivity Toolbox ──────────────────────────────────────────
    try:
        import bct

        print("[network] Computing clustering coefficients")
        cc = bct.clustering_coef_wu(W)
        metrics["mean_clustering"]    = float(np.mean(cc))
        metrics["std_clustering"]     = float(np.std(cc))

        print("[network] Computing global efficiency")
        ge = bct.efficiency_wei(W, local=False)
        metrics["global_efficiency"]  = float(ge)

        print("[network] Computing local efficiency")
        le = bct.efficiency_wei(W, local=True)
        metrics["mean_local_eff"]     = float(np.mean(le))

        print("[network] Computing betweenness centrality")
        bc = bct.betweenness_wei(1.0 / (W + 1e-9))
        metrics["hub_nodes"]          = np.argsort(bc)[-10:][::-1].tolist()

        print("[network] Community detection (modularity)")
        ci, q = bct.community_louvain(W)
        metrics["modularity_Q"]       = float(q)
        metrics["n_communities"]      = int(np.max(ci))

        print("[network] Computing small-worldness")
        metrics["mean_betweenness"]   = float(np.mean(bc))

    except ImportError:
        print("[network] [MOCK] bctpy not available — using NetworkX only")
        metrics["bct_available"] = False

    # ── NetworkX (always available) ─────────────────────────────────────────
    try:
        import networkx as nx

        print("[network] Building NetworkX graph")
        G = nx.from_numpy_array(W)

        metrics["n_nodes"]            = G.number_of_nodes()
        metrics["n_edges"]            = G.number_of_edges()
        metrics["density"]            = float(nx.density(G))

        # Degree centrality
        dc = nx.degree_centrality(G)
        hub_indices = sorted(dc, key=dc.get, reverse=True)[:10]
        metrics["degree_hubs"]        = hub_indices
        metrics["mean_degree"]        = float(np.mean([d for _, d in G.degree()]))

    except ImportError:
        print("[network] [MOCK] networkx not available")

    # ── Save results ────────────────────────────────────────────────────────
    out_json = output_dir / f"{subject_id}_network_metrics.json"
    out_json.write_text(json.dumps(metrics, indent=2))
    print(f"[network] Metrics saved: {out_json}")
    print(f"[network] Global efficiency: {metrics.get('global_efficiency', 'n/a'):.4f}")
    print(f"[network] Modularity Q:      {metrics.get('modularity_Q', 'n/a')}")
    print(f"[network] Communities:       {metrics.get('n_communities', 'n/a')}")
    print("[network] Done")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--subject", required=True)
    p.add_argument("--connectivity-dir", required=True, type=Path)
    p.add_argument("--tractography-dir", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    args = p.parse_args()
    run(args.subject, args.connectivity_dir, args.tractography_dir, args.output_dir)


if __name__ == "__main__":
    main()
