#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interpolate a radarlib CFRadial volume onto the Cartesian grid gl-tracker reads.

This is the gridding step the tracker's README places upstream of itself. It is
the only script here that imports radarlib; everything downstream works from the
NetCDF it produces.

    CFRadial .nc + geometry .npz  ->  Cartesian grid .nc  (DBZH + RHOHV)

Usage
-----
    python make_grid.py NETCDF [NETCDF ...] [options]

Options
    --geometry NPZ    Geometry file. Auto-discovered per radar when omitted.
    --geometry-root   Where to look for geometries (default: /workspace/geometry)
    --out-dir PATH    Output directory (default: /workspace/grids)
    --radar-name NAME Radar id; parsed from the filename when omitted.
    --referencia-z    "radar" (default) or "msnm". See grid_writer for why.
    --overwrite       Rewrite grids that already exist.

Both DBZH and RHOHV are required: the tracker's QC, its melting-layer mask and
its echo-top field all depend on RHOHV, and grilla.leer() rejects a volume whose
RHOHV is entirely absent.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("make_grid")

_TS_RE = re.compile(r"(\d{8}T\d{6})Z?")
_RADAR_RE = re.compile(r"((?:RMA|AR)\d+)", re.IGNORECASE)

REQUIRED_FIELDS = ("DBZH", "RHOHV")


def _parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("netcdf", nargs="+", type=Path, help="CFRadial NetCDF file(s)")
    p.add_argument("--geometry", type=Path, default=None)
    p.add_argument("--geometry-root", type=Path, default=Path("/workspace/geometry"))
    p.add_argument("--out-dir", type=Path, default=Path("/workspace/grids"))
    p.add_argument("--radar-name", default=None)
    p.add_argument("--referencia-z", default="radar", choices=["radar", "msnm"])
    p.add_argument("--overwrite", action="store_true")
    return p.parse_args()


def _radar_from_name(path: Path) -> str:
    m = _RADAR_RE.match(path.stem)
    if not m:
        raise ValueError(f"cannot infer radar name from {path.name}; pass --radar-name")
    return m.group(1).upper()


def _timestamp_from_name(path: Path) -> datetime:
    m = _TS_RE.search(path.stem)
    if not m:
        raise ValueError(f"no YYYYMMDDTHHMMSS timestamp in {path.name}")
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)


def _volume_from_name(path: Path) -> str | None:
    """RMA4_0315_01_2026...nc -> '01'. The geometry must match the volume."""
    parts = path.stem.split("_")
    return parts[2] if len(parts) >= 3 and parts[2].isdigit() else None


def _find_geometry(geo_root: Path, radar: str, volume: str | None) -> Path:
    """Locate the geometry for this radar, preferring an exact volume match.

    A geometry encodes a specific sweep layout, so a volume-02 geometry applied
    to a volume-01 file produces either an exception or a silently wrong grid.
    """
    radar_dir = geo_root / radar
    if not radar_dir.is_dir():
        raise FileNotFoundError(f"no geometry directory for {radar} under {geo_root}")

    candidates = sorted(radar_dir.glob("*geometry.npz"))
    if not candidates:
        raise FileNotFoundError(f"no *geometry.npz in {radar_dir}")

    if volume:
        exact = [p for p in candidates if f"_{volume}_" in p.name]
        if exact:
            return exact[0]
        raise FileNotFoundError(
            f"no geometry for {radar} volume {volume} in {radar_dir}; "
            f"found: {[p.name for p in candidates]}"
        )
    return candidates[0]


def _grid_coords(geometry):
    """Coordinate arrays for the grid, matching apply_geometry's index order."""
    import numpy as np

    nz, ny, nx = geometry.grid_shape
    (z_min, z_max), (y_min, y_max), (x_min, x_max) = geometry.grid_limits
    z_m = np.linspace(z_min, z_max, nz, dtype="float32")
    y_km = np.linspace(y_min, y_max, ny, dtype="float32") / 1000.0
    x_km = np.linspace(x_min, x_max, nx, dtype="float32") / 1000.0
    return z_m, y_km, x_km


def build_grid(
    netcdf_path: Path,
    geo_path: Path,
    out_dir: Path,
    radar_name: str,
    referencia_z: str,
    overwrite: bool,
) -> Path | None:
    import numpy as np
    from radarlib.io.pyart.pyart_radar import read_radar_netcdf
    from radarlib.radar_grid import apply_geometry_multi, load_geometry

    from grid_writer import write_cartesian_netcdf

    timestamp = _timestamp_from_name(netcdf_path)
    out_path = out_dir / f"{radar_name}_{timestamp.strftime('%Y%m%dT%H%M%S')}Z.nc"

    if out_path.exists() and not overwrite:
        logger.info(f"  exists, skipping (use --overwrite): {out_path.name}")
        return out_path

    logger.info(f"  geometry: {geo_path.name}")
    geometry = load_geometry(str(geo_path))
    logger.info(f"  grid shape: {geometry.grid_shape}")

    radar = read_radar_netcdf(str(netcdf_path))
    radar_lat = float(radar.latitude["data"].data[0])
    radar_lon = float(radar.longitude["data"].data[0])
    radar_alt_m = float(radar.altitude["data"].data[0])
    logger.info(
        f"  antenna: lat={radar_lat:.5f} lon={radar_lon:.5f} alt={radar_alt_m:.0f}m"
    )

    missing = [f for f in REQUIRED_FIELDS if f not in radar.fields]
    if missing:
        logger.error(
            f"  missing required field(s) {missing}; have {sorted(radar.fields)}"
        )
        return None

    # apply_geometry indexes a flat gate array and validates with
    # field_data.shape[0], so a (nrays, ngates) array is read as nrays gates.
    # get_gate_coordinates ravels in C order; match it.
    logger.info(f"  interpolating {list(REQUIRED_FIELDS)}...")
    grids = apply_geometry_multi(
        geometry,
        {name: radar.fields[name]["data"].ravel() for name in REQUIRED_FIELDS},
    )
    dbzh = np.ma.filled(grids["DBZH"], np.nan).astype(np.float32)
    rhohv = np.ma.filled(grids["RHOHV"], np.nan).astype(np.float32)

    finite = int(np.isfinite(dbzh).sum())
    if finite == 0:
        logger.error("  DBZH is entirely NaN after interpolation — nothing to write")
        return None
    pct = 100.0 * finite / dbzh.size
    logger.info(
        f"  DBZH coverage {pct:.2f}% ({finite:,} cells), "
        f"max={np.nanmax(dbzh):.1f} dBZ"
    )

    z_m, y_km, x_km = _grid_coords(geometry)
    write_cartesian_netcdf(
        path=out_path,
        dbzh=dbzh,
        rhohv=rhohv,
        x_km=x_km,
        y_km=y_km,
        z_m=z_m,
        radar_lat=radar_lat,
        radar_lon=radar_lon,
        radar_alt_m=radar_alt_m,
        timestamp=timestamp,
        referencia_z=referencia_z,
    )
    logger.info(f"  wrote {out_path} ({out_path.stat().st_size / 1e6:.1f} MB)")
    return out_path


def main() -> int:
    args = _parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    written, failed = [], []
    for netcdf_path in args.netcdf:
        netcdf_path = netcdf_path.expanduser().resolve()
        logger.info(f"{netcdf_path.name}")
        if not netcdf_path.is_file():
            logger.error("  not found")
            failed.append(netcdf_path)
            continue
        try:
            radar_name = args.radar_name or _radar_from_name(netcdf_path)
            geo_path = args.geometry or _find_geometry(
                args.geometry_root, radar_name, _volume_from_name(netcdf_path)
            )
            result = build_grid(
                netcdf_path, geo_path.expanduser().resolve(), out_dir,
                radar_name, args.referencia_z, args.overwrite,
            )
            (written if result else failed).append(netcdf_path)
        except Exception as e:
            logger.error(f"  {type(e).__name__}: {e}")
            failed.append(netcdf_path)

    print()
    print("=" * 70)
    print(f"grids written : {len(written)}")
    print(f"failed        : {len(failed)}")
    print(f"output dir    : {out_dir}")
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
