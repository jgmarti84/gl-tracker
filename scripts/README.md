# Running the tracker on real radar data

Tooling around `gl-tracker` that takes radar volumes from the FTP all the way to
a map you can look at. Everything runs in Docker; the only thing you need on the
host is `docker`.

```
BUFR (FTP) ──▶ CFRadial .nc ──▶ Cartesian grid .nc ──▶ SpatiaLite ──▶ HTML map
               fetch             grid                  track          map
```

---

## One-time setup

```bash
cd ~/gl-tracker
docker build -t gl-tracker:dev .
./scripts/pipeline.sh status        # should report "no grids found" — that is fine
```

To confirm the whole dependency stack is sound (GDAL, SpatiaLite, TATHU,
radarlib) run the checks:

```bash
docker run --rm -v ~/gl-tracker:/workspace/gl-tracker \
  -v ~/gl-radarlib:/workspace/gl-radarlib -v ~/geometry:/workspace/geometry:ro \
  -e PYTHONPATH=/workspace/gl-tracker:/workspace/gl-tracker/scripts:/workspace/gl-radarlib/src \
  gl-tracker:dev python scripts/verify_env.py
```

Expect `ALL 20 CHECKS PASSED` and `test_gate: OK`.

---

## Everyday use

**1. Find where the weather is.** Bar length is BUFR size, a good proxy for how
much echo a volume holds. It prints the largest volumes at the end.

```bash
./scripts/pipeline.sh survey RMA5
./scripts/pipeline.sh survey RMA8 2026/10/12      # any radar, any date
```

**2. Run the chain.** Pick a time window, a count, or explicit timestamps:

```bash
./scripts/pipeline.sh run RMA5 --date 2026/10/13 --from 14:00 --to 16:00
./scripts/pipeline.sh run RMA5 --latest 12
./scripts/pipeline.sh run RMA5 20261013T144420Z 20261013T145320Z
```

This fetches, grids, tracks, prints the systems table, builds the map, and
finishes with a coverage report.

**3. Look at it.**

```bash
./scripts/pipeline.sh open      # hands the file to the Windows browser (WSL)
./scripts/pipeline.sh serve     # or http://localhost:8900/tracking_map.html
```

---

## The map

| control | what it does |
|---|---|
| **← →** / slider / arrow keys | step through volumes |
| **All frames** | every frame at once — where tracks are easiest to see |
| **Radar** + slider | COLMAX reflectivity overlay and its opacity |
| **Forecasts** | the dashed +9…63 min projections |
| **Dark** / **Fit** | theme, and zoom back to the data |

Each track keeps **one colour across every frame**. A polygon that holds its
colour from one frame to the next is the same storm, associated by overlap —
those are marked `· tracked` in the legend. Click any polygon or table row for
its full diagnostics.

---

## When something looks wrong

```bash
./scripts/pipeline.sh status
```

Reports which requested volumes are missing and why, the typical cadence, and
any gap wide enough to reset tracking:

```
grids    : 11  2026-10-13 14:08:18 -> 15:56:26 UTC
missing  : 2 of 13 requested
   14:53:20  never fetched (FTP failure?)
   15:11:22  fetched but failed to grid (incomplete scan?)
cadence  : 9.0 min typical
gaps     : none exceed 20 min — tracking runs unbroken
```

Re-fetch anything missing and re-run; `grid` skips what it already has:

```bash
./scripts/pipeline.sh fetch RMA5 20261013T145320Z
./scripts/pipeline.sh grid RMA5 && sleep 32 && ./scripts/pipeline.sh track
./scripts/pipeline.sh map
```

**Dropouts are worth repairing.** Overlap tracking needs the full ~9-minute
cadence. One missing volume doubles the interval, and storms can move too far to
overlap themselves — the track dies even when the gap is still under
`max_gap_tracking_min`.

---

## Things that will trip you up

- **Grids must be 30 s old** before the tracker looks at them
  (`Config.grilla_edad_minima`). `run` sleeps for this; separate `track` calls
  may need a `sleep 32`.
- **`clean` keeps the downloaded CFRadial files** and only clears `~/grids` and
  `~/tracking_out`, so re-running costs no FTP time.
- **Budget ~25 s per volume**, nearly all of it download, plus ~13 MB per
  CFRadial and ~15 MB per grid on disk.
- **Detection thresholds are tuned for RMA1** organized convection (CAPPI at
  2500 m, 35 dBZ, 50 km²). Scattered cells on other radars often detect little
  or nothing. That is a tuning conversation for the meteorology team, not a bug.
- **H0 falls back to climatology** until a WRF directory is mounted at
  `WRFOUT_RAIZ`.

---

## Directories

| path | holds |
|---|---|
| `~/radar_data` | CFRadial NetCDFs (polar volumes) |
| `~/grids` | Cartesian grids — what the tracker reads |
| `~/tracking_out` | `systems-db.sqlite`, `estado_tracking.pkl`, `tracking_map.html` |
| `~/geometry` | interpolation geometries, read-only |

A radar works as long as `~/geometry/<RADAR>/` has a volume-01 geometry.
Currently: RMA4, RMA5, RMA8, AR8.

---

## The scripts, individually

| script | role |
|---|---|
| `pipeline.sh` | the wrapper you actually call |
| `list_volumes.py` | resolves `--latest` / `--from…--to` into timestamps |
| `fetch_radar_volume.py` | FTP BUFR → CFRadial NetCDF |
| `make_grid.py` | CFRadial + geometry → Cartesian grid |
| `grid_writer.py` | the grid NetCDF format contract |
| `export_map.py` | SpatiaLite → standalone HTML map |
| `status.py` | coverage and tracking-gap report |
| `verify_env.py` | dependency checks |

`radarlib` is **not** baked into the image — it is bind-mounted, so edits there
take effect with no rebuild.
