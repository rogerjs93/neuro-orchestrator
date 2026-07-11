"""Uniform surface remeshing for cleaner, print-ready STL meshes.

Marching-cubes meshes from brain masks are dense and irregular (staircase artifacts,
highly variable triangle sizes). This step produces a uniform, isotropic triangle mesh
via ACVD clustering (pyacvd on top of pyvista/VTK) — the retopology that makes an STL
look clean and slice well, without leaving the Python process (no external binary, no Qt).

Design notes:
  * Headless: pyacvd/pyvista/VTK only — chosen over huxingyi/autoremesher (Qt GUI stack,
    OBJ/quad output) because it drops straight into this pipeline as a function call.
  * Best-effort: if pyacvd/pyvista are not installed, or remeshing fails, the ORIGINAL mesh
    is returned untouched with a reason in the report — export is never blocked.
  * Runs AFTER topology repair (watertight input clusters better); the caller should
    re-run repair_mesh afterwards since clustering can reopen small boundaries.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import numpy as np
import trimesh


def _trimesh_to_polydata(mesh: "trimesh.Trimesh"):
    import pyvista as pv

    faces = np.hstack(
        [np.full((len(mesh.faces), 1), 3, dtype=np.int64), mesh.faces.astype(np.int64)]
    ).ravel()
    return pv.PolyData(np.asarray(mesh.vertices, dtype=float), faces)


def _polydata_to_trimesh(pd) -> "trimesh.Trimesh":
    faces = pd.faces.reshape(-1, 4)[:, 1:]  # ACVD output is all-triangle
    return trimesh.Trimesh(vertices=np.asarray(pd.points), faces=faces, process=False)


def remesh_mesh(
    mesh: "trimesh.Trimesh",
    *,
    target_vertices: Optional[int] = None,
    target_ratio: float = 1.0,
    min_faces: int = 1000,
    max_subdivisions: int = 3,
) -> Tuple["trimesh.Trimesh", Dict[str, Any]]:
    """Return a uniformly remeshed copy of ``mesh`` (+ a report). Never raises.

    Parameters
    ----------
    target_vertices : explicit output vertex budget. If None, uses
        ``max(500, round(len(mesh.vertices) * target_ratio))``.
    target_ratio : output/input vertex ratio when ``target_vertices`` is None
        (1.0 = same density, uniform; <1.0 = lighter, coarser).
    min_faces : skip remeshing for meshes smaller than this (nothing to gain).
    max_subdivisions : cap on pre-clustering subdivision (ACVD needs the input denser
        than the cluster count; each subdivision ~4x the triangles).
    """
    report: Dict[str, Any] = {
        "remeshed": False,
        "before": {"vertices": int(len(mesh.vertices)), "faces": int(len(mesh.faces))},
    }

    if len(mesh.faces) < min_faces:
        report["reason"] = f"mesh below min_faces ({len(mesh.faces)} < {min_faces})"
        return mesh, report

    try:
        import pyacvd  # noqa: F401  (import guarded so absence is non-fatal)
    except Exception as exc:  # pragma: no cover - environment-dependent
        report["reason"] = f"pyacvd unavailable ({exc}); remesh skipped"
        return mesh, report

    try:
        n_target = (
            int(target_vertices)
            if target_vertices
            else max(500, int(round(len(mesh.vertices) * target_ratio)))
        )

        clus = pyacvd.Clustering(_trimesh_to_polydata(mesh))

        # ACVD needs the input mesh denser than the requested cluster count.
        subdivisions = 0
        while clus.mesh.n_points < n_target * 2 and subdivisions < max_subdivisions:
            clus.subdivide(1)
            subdivisions += 1

        clus.cluster(n_target)
        out = _polydata_to_trimesh(clus.create_mesh())

        report.update(
            remeshed=True,
            target_vertices=n_target,
            subdivisions=subdivisions,
            after={"vertices": int(len(out.vertices)), "faces": int(len(out.faces))},
            method="acvd",
        )
        return out, report
    except Exception as exc:  # pragma: no cover - defensive
        report["reason"] = f"remesh failed ({exc}); original mesh kept"
        return mesh, report
