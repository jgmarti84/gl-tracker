# -*- coding: utf-8 -*-
"""Write a Cartesian radar volume to NetCDF4 in the format gl-tracker expects.

Deliberately standalone — numpy and netCDF4 only, no radarlib import — so the
same writer can be called from this repo's scripts and, later, from radarlib's
ProductGenerationDaemon without creating a dependency cycle.

Row order
---------
radarlib's apply_geometry returns grid[iz, iy, ix] with y increasing northward,
so row 0 is the SOUTHERNMOST row. gl-tracker follows the GDAL convention where
row 0 is NORTH. This module flips the data on write and leaves y_km in its
original south-to-north order, because the tracker's _build_raster takes
y_m[-1] (the northernmost edge) as the GDAL origin with a negative dy step.

Because the flip happens here, the tracker must run with Config.invertir_y =
False. Flipping in both places silently mirrors the grid north-south.

Vertical reference
------------------
referencia_z defaults to "radar" — heights above the antenna, not above sea
level. The chain that makes this so:

  1. radar_grid.utils.get_gate_coordinates returns radar.gate_z, which Py-ART
     defines as the gate height relative to the radar (gate_altitude is the
     absolute one).
  2. radar_grid.compute.compute_grid_geometry computes
     gate_z_relative = gate_z - radar_altitude, and every geometry in this
     project is built with radar_altitude=0, making that a no-op.

So the grid's z axis stays antenna-relative, and the tracker converts H0 into
that frame by subtracting the antenna altitude (gate.h0_para_grilla).

This is the one input attribute that fails silently when wrong: a mistaken
"msnm" shifts every echo top by the antenna altitude against a C2 margin of
1500 m. Writing the attribute explicitly is what closes that hole, so always
pass the value that matches the geometry rather than relying on the default.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from netCDF4 import Dataset

_FILL = np.float32(-9999.0)  # finite sentinel; tracker maps it back to NaN


def write_cartesian_netcdf(
    path: Path,
    dbzh: np.ndarray,
    rhohv: np.ndarray,
    x_km: np.ndarray,
    y_km: np.ndarray,
    z_m: np.ndarray,
    radar_lat: float,
    radar_lon: float,
    radar_alt_m: float,
    timestamp: datetime,
    referencia_z: str = "radar",
) -> Path:
    """Write one Cartesian radar volume as NetCDF4.

    Parameters
    ----------
    path          output file path; parent directories are created
    dbzh, rhohv   (nz, ny, nx) float32, NaN = no data, row 0 = SOUTH on input
    x_km          (nx,) km from the radar, increasing eastward
    y_km          (ny,) km from the radar, increasing northward, y[0] southernmost
    z_m           (nz,) height in metres in the frame named by referencia_z
    radar_lat     antenna latitude
    radar_lon     antenna longitude
    radar_alt_m   antenna altitude above sea level; the tracker uses it to put
                  H0 into the grid's vertical frame
    timestamp     volume observation time; naive values are read as UTC
    referencia_z  "radar" (above antenna) or "msnm" (above sea level)

    Returns the resolved output path.
    """
    if referencia_z not in ("radar", "msnm"):
        raise ValueError(f"referencia_z must be 'radar' or 'msnm', got {referencia_z!r}")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    dbzh = np.ma.filled(np.asarray(dbzh, dtype=np.float32), np.nan)
    rhohv = np.ma.filled(np.asarray(rhohv, dtype=np.float32), np.nan)

    if dbzh.shape != rhohv.shape:
        raise ValueError(f"shape mismatch: DBZH {dbzh.shape} vs RHOHV {rhohv.shape}")
    if dbzh.ndim != 3:
        raise ValueError(f"expected (nz, ny, nx), got {dbzh.shape}")

    # row 0 south (radarlib) -> row 0 north (tracker/GDAL)
    dbzh = dbzh[:, ::-1, :]
    rhohv = rhohv[:, ::-1, :]

    nz, ny, nx = dbzh.shape
    x_km = np.asarray(x_km, dtype=np.float32)
    y_km = np.asarray(y_km, dtype=np.float32)  # left south-to-north on purpose
    z_m = np.asarray(z_m, dtype=np.float32)

    if (len(z_m), len(y_km), len(x_km)) != (nz, ny, nx):
        raise ValueError(
            f"coordinate lengths {(len(z_m), len(y_km), len(x_km))} "
            f"do not match data shape {(nz, ny, nx)}"
        )

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    with Dataset(str(path), "w", format="NETCDF4") as nc:
        nc.createDimension("z", nz)
        nc.createDimension("y", ny)
        nc.createDimension("x", nx)

        vz = nc.createVariable("z_m", "f4", ("z",))
        vz[:] = z_m
        vz.units = "m"
        vz.long_name = f"Height above {'antenna' if referencia_z == 'radar' else 'sea level'}"

        vy = nc.createVariable("y_km", "f4", ("y",))
        vy[:] = y_km
        vy.units = "km"
        vy.long_name = "Distance from radar northward (y[0]=south, y[-1]=north)"

        vx = nc.createVariable("x_km", "f4", ("x",))
        vx[:] = x_km
        vx.units = "km"
        vx.long_name = "Distance from radar eastward"

        def _field(name, data, long_name, units):
            v = nc.createVariable(
                name, "f4", ("z", "y", "x"),
                fill_value=_FILL, zlib=True, complevel=1,
            )
            v[:] = np.where(np.isfinite(data), data, _FILL)
            v.long_name = long_name
            v.units = units

        _field("DBZH", dbzh, "Horizontal reflectivity factor", "dBZ")
        _field("RHOHV", rhohv, "Cross-correlation ratio", "1")

        nc.referencia_z = referencia_z
        nc.timestamp = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        nc.radar_lat = float(radar_lat)
        nc.radar_lon = float(radar_lon)
        nc.radar_alt_m = float(radar_alt_m)
        nc.row_order = "north_first"
        nc.Conventions = "CF-1.8"
        nc.history = "Created by gl-tracker scripts/grid_writer.py"

    return path
