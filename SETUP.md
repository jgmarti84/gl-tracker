# Setup and configuration

Everything runs inside Docker. The only host requirements are `docker` and
`git`. No Python installation is needed on the host.

---

## 1. Clone and build

```bash
git clone https://github.com/jgmarti84/gl-tracker.git
cd gl-tracker
docker build -t gl-tracker:dev .
```

The build takes a few minutes the first time (GDAL, TATHU). Subsequent builds
are cached.

---

## 2. FTP credentials

Credentials are **not** stored in the repository. Set them as environment
variables before running any command:

```bash
export FTP_HOST=200.16.116.24
export FTP_USER=your_user
export FTP_PASS=your_password
```

Put those three lines in your `~/.bashrc` (or `~/.zshrc`) so they persist
across sessions. `pipeline.sh` forwards them into the container automatically.

To verify the FTP is reachable:

```bash
./scripts/pipeline.sh survey RMA5
```

---

## 3. Host directories

By default the pipeline reads and writes to these directories on the host:

| env var | default | holds |
|---|---|---|
| `GEOMETRY_DIR` | `~/geometry` | interpolation geometry NPZ files (read-only) |
| `RADAR_DATA_DIR` | `~/radar_data` | CFRadial NetCDFs (polar volumes) |
| `GRIDS_DIR` | `~/grids` | Cartesian grids (tracker input) |
| `TRACKING_OUT_DIR` | `~/tracking_out` | SQLite database, tracker state, HTML map |
| `RADARLIB_DIR` | `~/gl-radarlib` | radarlib source (bind-mounted, not baked in) |

Override any of them by setting the env var before running `pipeline.sh`:

```bash
export RADARLIB_DIR=/opt/gl-radarlib
export GEOMETRY_DIR=/data/sinarame/geometry
./scripts/pipeline.sh survey RMA5
```

Or inline for a single run:

```bash
GRIDS_DIR=/data/grids TRACKING_OUT_DIR=/data/out ./scripts/pipeline.sh track
```

---

## 4. Geometry files

Each radar needs one geometry NPZ file (produced by radarlib's
`compute_grid_geometry`). The pipeline looks for it at:

```
$GEOMETRY_DIR/<RADAR>/<RADAR>_<strategy>_<vol-nr>_*_geometry.npz
```

For example:

```
~/geometry/
  RMA5/
    RMA5_0315_01_RES1000x600_..._geometry.npz
  RMA1/
    RMA1_0315_01_RES1000x600_..._geometry.npz
```

These files are **not** in the repository (they are large and radar-specific).
Copy them from the machine where they were generated, or generate them with
radarlib's `compute_grid_geometry` utility.

A radar without a geometry file will fail at the `grid` step. All other steps
(`fetch`, `survey`, `list`) work without it.

---

## 5. Using existing NetCDF files

If you already have CFRadial NetCDF volumes on disk, skip the `fetch` step and
point `RADAR_DATA_DIR` at the directory that holds them:

```bash
export RADAR_DATA_DIR=/data/my_existing_netcdfs
./scripts/pipeline.sh grid RMA5
```

The `grid` command processes every `RMA5_*.nc` it finds in that directory.
Files that have already been gridded are skipped (the output grid already
exists in `$GRIDS_DIR`).

Filename format expected: `<RADAR>_<strategy>_<vol-nr>_<timestamp>.nc`, e.g.
`RMA5_0315_01_20261013T144420Z.nc`. The timestamp is parsed from the filename
by regex, so the rest of the name can vary.

---

## 6. radarlib

radarlib is bind-mounted into the container at `/workspace/gl-radarlib`. It is
NOT baked into the image, so edits to radarlib take effect immediately without
rebuilding.

Set `RADARLIB_DIR` to wherever your clone lives:

```bash
export RADARLIB_DIR=~/gl-radarlib    # default
export RADARLIB_DIR=/opt/gl-radarlib # alternative
```

---

## 7. Quick smoke test

After setting credentials and copying geometry files, run the dependency
checker to confirm the environment is sound:

```bash
docker run --rm \
  -v ~/gl-tracker:/workspace/gl-tracker \
  -v ~/gl-radarlib:/workspace/gl-radarlib \
  -v "${GEOMETRY_DIR:-$HOME/geometry}:/workspace/geometry:ro" \
  -e PYTHONPATH=/workspace/gl-tracker:/workspace/gl-tracker/scripts:/workspace/gl-radarlib/src \
  gl-tracker:dev python scripts/verify_env.py
```

Expect `ALL 20 CHECKS PASSED` and `test_gate: OK`.

---

## 8. Typical first run

```bash
# 1. Find a time window with convection
./scripts/pipeline.sh survey RMA5

# 2. Fetch, grid, track, and view the result
./scripts/pipeline.sh run RMA5 --date 2026/10/13 --from 14:00 --to 18:00

# 3. Open the map
./scripts/pipeline.sh open          # Windows browser via WSL
./scripts/pipeline.sh serve         # or: http://localhost:8900/tracking_map.html
```

See `scripts/README.md` for the full command reference and map controls.

---

## 9. All configurable env vars

| variable | default | description |
|---|---|---|
| `FTP_HOST` | *(required)* | FTP server address |
| `FTP_USER` | *(required)* | FTP username |
| `FTP_PASS` | *(required)* | FTP password |
| `GEOMETRY_DIR` | `~/geometry` | geometry NPZ files root |
| `RADAR_DATA_DIR` | `~/radar_data` | CFRadial NetCDF input/output |
| `GRIDS_DIR` | `~/grids` | Cartesian grid output |
| `TRACKING_OUT_DIR` | `~/tracking_out` | database, state, map output |
| `RADARLIB_DIR` | `~/gl-radarlib` | radarlib source directory |
| `GL_TRACKER_IMAGE` | `gl-tracker:dev` | Docker image tag to use |
