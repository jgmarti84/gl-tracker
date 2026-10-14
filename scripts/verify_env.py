#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify the gl-tracker container has everything the pipeline needs.

Run this first, before touching radar data. Every check here corresponds to a
failure that is either silent or only surfaces deep into a run:

  * mod_spatialite missing  -> tathu creates no output database at all
  * rtree without libspatialindex -> ImportError inside tathu.tracking.system
  * radarlib not on PYTHONPATH -> the gridding step cannot run
  * geometry radar_altitude != 0 -> the vertical reference silently shifts

Exits 0 if everything the tracker needs is present, 1 otherwise.
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

RESULTS: list[tuple[str, bool, str]] = []
PAD = 34


def check(label: str, fn) -> bool:
    """Run one check; record and print its outcome."""
    try:
        detail = fn() or "ok"
        RESULTS.append((label, True, str(detail)))
        print(f"  [ OK ] {label:<{PAD}} {detail}")
        return True
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        RESULTS.append((label, False, msg))
        print(f"  [FAIL] {label:<{PAD}} {msg}")
        return False


def _import(mod: str, attr: str | None = None):
    """Return a check function that imports `mod` and reports its version."""

    def run():
        m = importlib.import_module(mod)
        if attr:
            getattr(m, attr)
        return getattr(m, "__version__", "") or "imported"

    return run


# ---------------------------------------------------------------------------
# 1. GDAL and the system libraries that pip cannot provide
# ---------------------------------------------------------------------------

def check_gdal():
    from osgeo import gdal, ogr, osr  # noqa: F401

    return f"GDAL {gdal.__version__}"


def check_spatialite():
    """tathu.io.spatialite calls load_extension('mod_spatialite').

    Two things can break here: the Python build may be compiled without
    extension loading, or the .so may be absent. Both produce a tracker that
    runs to completion and writes nothing.
    """
    import sqlite3

    con = sqlite3.connect(":memory:")
    try:
        if not hasattr(con, "enable_load_extension"):
            raise RuntimeError("python sqlite3 built without load_extension support")
        con.enable_load_extension(True)
        con.load_extension("mod_spatialite")
        ver = con.execute("SELECT spatialite_version()").fetchone()[0]
        return f"mod_spatialite {ver}"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 2. The tathu modules gl-tracker actually imports
# ---------------------------------------------------------------------------

def check_tathu_surface():
    """Import the exact tathu surface pipeline.py and grilla.py reach for."""
    from tathu.constants import KM_PER_DEGREE, METERS_PER_DEGREE  # noqa: F401
    from tathu.geometry import transform  # noqa: F401
    from tathu.io import spatialite  # noqa: F401
    from tathu.tracking import descriptors, detectors, forecasters, trackers  # noqa: F401
    from tathu.tracking.system import LifeCycleEvent  # noqa: F401

    import tathu

    return f"tathu {getattr(tathu, '__version__', '?')} (tracking surface ok)"


# ---------------------------------------------------------------------------
# 3. radarlib, for the gridding step
# ---------------------------------------------------------------------------

def check_radarlib():
    from radarlib.io.pyart.pyart_radar import read_radar_netcdf  # noqa: F401
    from radarlib.radar_grid import apply_geometry_multi, load_geometry  # noqa: F401

    import radarlib

    return f"radarlib from {Path(radarlib.__file__).parent}"


# ---------------------------------------------------------------------------
# 4. gl-tracker's own modules
# ---------------------------------------------------------------------------

def check_tracker_modules():
    import config, estado, filtro_convectivo, gate, grilla, pipeline  # noqa: F401

    cfg = filtro_convectivo.ConvectiveFilterConfig()
    return (
        f"ok (filter: rhohv_qc_min={cfg.rhohv_qc_min}, "
        f"echotop_dbz={cfg.echotop_dbz}, r_max_km={cfg.r_max_km})"
    )


# ---------------------------------------------------------------------------
# 5. Geometry files and their vertical reference
# ---------------------------------------------------------------------------

def check_geometry(geo_root: Path):
    """Report available geometries and flag the vertical-reference assumption.

    radar.gate_z from Py-ART is height above the antenna, and compute_grid_geometry
    is called with radar_altitude=0, so the grid z axis stays antenna-relative.
    That means referencia_z="radar". If a geometry ever carries a non-zero
    radar_altitude, that conclusion no longer holds and the grid writer and the
    tracker config both have to change together.
    """
    import numpy as np

    if not geo_root.is_dir():
        raise FileNotFoundError(f"{geo_root} not mounted")

    found = sorted(geo_root.glob("*/*geometry.npz"))
    if not found:
        raise FileNotFoundError(f"no *geometry.npz under {geo_root}")

    lines = []
    nonzero_alt = []
    for p in found:
        with np.load(p, allow_pickle=True) as d:
            shape = tuple(int(v) for v in d["grid_shape"])
            zmin, zmax = (float(v) for v in d["grid_limits_z"])
            alt = float(np.ravel(d["radar_altitude"])[0])
        lines.append(f"{p.parent.name}/{p.name[:28]}… shape={shape} z=[{zmin:.0f},{zmax:.0f}] alt={alt:.0f}")
        if alt != 0.0:
            nonzero_alt.append(p.name)

    print()
    for ln in lines:
        print(f"         {ln}")
    print()

    if nonzero_alt:
        raise RuntimeError(
            f"radar_altitude != 0 in {nonzero_alt} — vertical reference assumption broken"
        )
    return f"{len(found)} geometry file(s), all radar_altitude=0 -> referencia_z='radar'"


# ---------------------------------------------------------------------------
# 6. The gate property test that guards the tracker's core invariant
# ---------------------------------------------------------------------------

def run_gate_test(tracker_root: Path) -> bool:
    test = tracker_root / "test_gate.py"
    if not test.is_file():
        print(f"  [SKIP] test_gate.py not found at {test}")
        return True

    print("\n--- test_gate.py " + "-" * 52)
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(test)],
        cwd=str(tracker_root),
        capture_output=True,
        text=True,
    )
    out = (proc.stdout + proc.stderr).strip()
    for line in out.splitlines():
        print(f"  {line}")
    ok = proc.returncode == 0
    print(f"--- test_gate.py: {'PASS' if ok else 'FAIL'} " + "-" * 45)
    return ok


# ---------------------------------------------------------------------------

def main() -> int:
    tracker_root = Path(__file__).resolve().parent.parent
    geo_root = Path("/workspace/geometry")

    print("=" * 72)
    print("gl-tracker environment verification")
    print("=" * 72)
    print(f"python   : {sys.version.split()[0]}")
    print(f"tracker  : {tracker_root}")
    print()

    print("System libraries")
    check("GDAL / osgeo", check_gdal)
    check("SpatiaLite extension", check_spatialite)

    print("\ntathu and its transitive deps")
    check("tathu (gl-tracker surface)", check_tathu_surface)
    for mod in ("numpy", "scipy", "skimage", "cv2", "rtree", "shapely",
                "rasterstats", "affine", "pathos", "netCDF4"):
        check(mod, _import(mod))

    print("\nradarlib (gridding step)")
    check("radarlib", check_radarlib)
    check("pyart", _import("pyart"))

    print("\ngl-tracker modules")
    check("config/gate/grilla/pipeline", check_tracker_modules)

    print("\nMounted data")
    check("geometry (.npz)", lambda: check_geometry(geo_root))
    for label, path in (
        ("radar_data (CFRadial .nc)", "/workspace/radar_data"),
        ("grids (Cartesian .nc)", "/workspace/grids"),
        ("tracking_out", "/workspace/tracking_out"),
    ):
        check(label, lambda p=path: (
            f"{len(list(Path(p).glob('**/*.nc')))} .nc file(s)"
            if Path(p).is_dir() else (_ for _ in ()).throw(FileNotFoundError(f"{p} not mounted"))
        ))

    gate_ok = run_gate_test(tracker_root)

    failed = [label for label, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 72)
    if failed or not gate_ok:
        print(f"FAILED  {len(failed)} check(s): {', '.join(failed) or '-'}"
              f"{'  + test_gate' if not gate_ok else ''}")
        print("=" * 72)
        return 1

    print(f"ALL {len(RESULTS)} CHECKS PASSED — environment ready")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
