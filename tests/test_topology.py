import numpy as np

from pipeline.topology import (
    mask_topology,
    repair_mask_topology,
    mesh_topology,
    repair_mesh,
)


def test_solid_ball_is_genus_zero(ball):
    t = mask_topology(ball((40, 40, 40), (20, 20, 20), 12))
    assert t["n_components"] == 1
    assert t["n_cavities"] == 0
    assert t["genus"] == 0


def test_hollow_shell_has_one_cavity(ball):
    shell = ball((40, 40, 40), (20, 20, 20), 12) & ~ball((40, 40, 40), (20, 20, 20), 6)
    t = mask_topology(shell)
    assert t["n_cavities"] == 1


def test_two_balls_two_components(ball):
    two = ball((40, 80, 40), (20, 20, 20), 10) | ball((40, 80, 40), (20, 60, 20), 10)
    assert mask_topology(two)["n_components"] == 2


def test_torus_is_genus_one():
    zz, yy, xx = np.ogrid[:40, :60, :60]
    R, r = 16, 5
    d = np.sqrt((yy - 30) ** 2 + (xx - 30) ** 2)
    torus = ((R - d) ** 2 + (zz - 20) ** 2) <= r * r
    assert mask_topology(torus)["genus"] == 1


def test_repair_removes_islands_and_fills_cavities(ball):
    S = (40, 80, 40)
    big = ball(S, (20, 20, 20), 12) & ~ball(S, (20, 20, 20), 6)  # shell w/ cavity
    island = ball(S, (20, 60, 20), 5)
    repaired, report = repair_mask_topology(big | island)
    assert report["removed_components"] == 1
    assert report["filled_cavities"] == 1
    after = mask_topology(repaired)
    assert after["n_components"] == 1
    assert after["n_cavities"] == 0


def test_mesh_topology_icosphere():
    import trimesh
    sph = trimesh.creation.icosphere(subdivisions=2, radius=1.0)
    t = mesh_topology(sph)
    assert t["watertight"] is True
    assert t["genus"] == 0
    assert t["euler_number"] == 2


def test_repair_mesh_runs_without_error():
    import trimesh
    sph = trimesh.creation.icosphere(subdivisions=2, radius=1.0)
    broken = trimesh.Trimesh(vertices=sph.vertices, faces=sph.faces[:-10], process=False)
    fixed, report = repair_mesh(broken)
    assert "before" in report and "after" in report
    assert len(fixed.faces) > 0
