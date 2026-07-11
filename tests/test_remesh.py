import numpy as np
import pytest
import trimesh

from pipeline.remesh import remesh_mesh

pyacvd = pytest.importorskip("pyacvd", reason="pyacvd not installed (optional remesh dep)")


def _edge_cv(mesh):
    """Coefficient of variation of edge lengths — lower means more uniform triangles."""
    el = mesh.edges_unique_length
    return float(np.std(el) / np.mean(el))


def test_remesh_hits_target_vertex_budget():
    m = trimesh.creation.icosphere(subdivisions=4)  # ~2562 verts, >min_faces
    out, rep = remesh_mesh(m, target_vertices=1500)
    assert rep["remeshed"] is True
    assert rep["method"] == "acvd"
    # ACVD clusters to ~target vertices (allow a small tolerance)
    assert abs(len(out.vertices) - 1500) <= 50
    assert out.is_winding_consistent


def test_remesh_improves_uniformity_on_irregular_mesh():
    # Build an irregular mesh: a coarse sphere unevenly refined on one hemisphere.
    m = trimesh.creation.icosphere(subdivisions=3)
    top = m.vertices[:, 2] > 0
    face_top = top[m.faces].any(axis=1)
    dense = m.submesh([face_top], append=True).subdivide().subdivide()
    coarse = m.submesh([~face_top], append=True)
    irregular = trimesh.util.concatenate([dense, coarse])
    before = _edge_cv(irregular)
    out, rep = remesh_mesh(irregular, target_ratio=1.0)
    assert rep["remeshed"] is True
    assert _edge_cv(out) < before  # more uniform after remeshing


def test_remesh_skips_small_mesh():
    out, rep = remesh_mesh(trimesh.creation.box())
    assert rep["remeshed"] is False
    assert "min_faces" in rep["reason"]
    assert out is not None and len(out.faces) == 12  # original returned untouched
