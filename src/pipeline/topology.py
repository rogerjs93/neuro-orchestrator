"""
Topology analysis + repair for tissue masks and meshes.

Interprets the topology of a binary mask — connectivity (components), enclosed
cavities, and handles (genus, via the Euler characteristic) — to:
  (a) QC mask quality at the review gate, and
  (b) repair masks/meshes so exported STLs are watertight and printable.

A clean solid mask is: one connected component, no internal cavities, genus 0
(no handles) → a watertight, genus-0 surface. Note that some cavities are real
anatomy (e.g. ventricles), so cavities/genus are reported as signal, not treated
as automatic failures.

For a binary volume with b0 connected components, b2 enclosed cavities and b1
independent handles, the Euler characteristic is chi = b0 - b1 + b2, so the
total handle count (genus) estimates as b1 = b0 + b2 - chi.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

import numpy as np


def mask_topology(mask: "np.ndarray") -> Dict[str, Any]:
    """Topological descriptors of a binary volume (best-effort; never raises)."""
    out: Dict[str, Any] = {}
    try:
        from scipy.ndimage import label, binary_fill_holes, generate_binary_structure
        m = np.asarray(mask) > 0
        out["voxel_count"] = int(m.sum())
        if not m.any():
            out.update({"n_components": 0, "n_cavities": 0, "euler_number": 0, "genus": 0})
            return out

        struct = generate_binary_structure(3, 3)  # 26-connectivity
        _, n_comp = label(m, structure=struct)
        out["n_components"] = int(n_comp)

        filled = binary_fill_holes(m)
        cavities = filled & ~m
        n_cav = int(label(cavities, structure=struct)[1]) if cavities.any() else 0
        out["n_cavities"] = n_cav

        try:
            from skimage.measure import euler_number
            chi = int(euler_number(m, connectivity=3))
            out["euler_number"] = chi
            out["genus"] = int(max(0, n_comp + n_cav - chi))
        except Exception:
            out["euler_number"] = None
            out["genus"] = None
    except Exception as exc:  # pragma: no cover - defensive
        out["error"] = str(exc)
    return out


def repair_mask_topology(
    mask: "np.ndarray",
    *,
    keep_largest: bool = True,
    fill_cavities: bool = True,
) -> Tuple["np.ndarray", Dict[str, Any]]:
    """Return a topologically cleaner mask (largest component, cavities filled) + report."""
    from scipy.ndimage import label, binary_fill_holes, generate_binary_structure

    m = np.asarray(mask) > 0
    before = mask_topology(m)
    struct = generate_binary_structure(3, 3)

    if keep_largest and m.any():
        lbl, n = label(m, structure=struct)
        if n > 1:
            counts = np.bincount(lbl.ravel())
            counts[0] = 0
            m = lbl == int(counts.argmax())
    if fill_cavities and m.any():
        m = binary_fill_holes(m)

    after = mask_topology(m)
    report = {
        "before": before,
        "after": after,
        "removed_components": int(before.get("n_components", 0) - after.get("n_components", 0)),
        "filled_cavities": int(before.get("n_cavities", 0) - after.get("n_cavities", 0)),
    }
    return m.astype(np.uint8), report


def mesh_topology(mesh: Any) -> Dict[str, Any]:
    """Topological descriptors of a trimesh.Trimesh (best-effort)."""
    out: Dict[str, Any] = {}
    try:
        out["watertight"] = bool(mesh.is_watertight)
        out["winding_consistent"] = bool(mesh.is_winding_consistent)
        out["euler_number"] = int(mesh.euler_number)
        out["n_vertices"] = int(len(mesh.vertices))
        out["n_faces"] = int(len(mesh.faces))
        bodies = int(getattr(mesh, "body_count", 1) or 1)
        out["n_bodies"] = bodies
        # Closed orientable surface(s): chi = 2*components - 2*genus_total.
        out["genus"] = (
            int(max(0, (2 * bodies - out["euler_number"]) // 2)) if out["watertight"] else None
        )
    except Exception as exc:  # pragma: no cover - defensive
        out["error"] = str(exc)
    return out


def repair_mesh(mesh: Any) -> Tuple[Any, Dict[str, Any]]:
    """Best-effort make a mesh watertight/manifold for printing. Returns (mesh, report).

    Uses trimesh 4.x-safe calls, each guarded so API/version differences are
    non-fatal — we only ever improve printability, never block export.
    """
    before = mesh_topology(mesh)
    steps = (
        lambda: mesh.update_faces(mesh.nondegenerate_faces()),
        lambda: mesh.update_faces(mesh.unique_faces()),
        lambda: mesh.remove_unreferenced_vertices(),
        lambda: (mesh.fill_holes() if not mesh.is_watertight else None),
        lambda: mesh.fix_normals(),
    )
    for step in steps:
        try:
            step()
        except Exception:
            pass
    after = mesh_topology(mesh)
    return mesh, {"before": before, "after": after}
