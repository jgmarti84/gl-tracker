# AGENTS.md — gl-tracker

Instructions for anyone working on this repo, human or AI agent (Claude, Codex,
Copilot, …). The detailed reference is [`README.md`](README.md) (in Spanish) and
[`SETUP.md`](SETUP.md).

## What this is
Convective storm tracking (TATHU-based) with a volume gate on C2: a whole radar volume
is skipped unless some echo top reaches above the WRF-forecast 0 °C level (H0) plus a
margin. Input is an already-interpolated Cartesian radar grid; output is tracked
systems and forecasts. Tuned and validated on **RMA1**.

It is a **Python library and tooling, not a deployed service**. The planned
integration is a slim tracking sidecar on the gl-webmet25 side, fed by gl-radarlib's
3D "NC cube" grids.

## Working rules (the same in gl-radarlib, gl-webmet25 and gl-tracker)
1. **Branch from `origin/dev`; open PRs into `dev`.** Never push to `dev` or `main`
   directly. `dev → main` is a separate promotion done by the maintainer. Agents never
   merge PRs.
2. **Propose → approve → apply.** For anything non-trivial, describe the change and
   wait for approval before writing it.
3. **Secrets never go into git.** Credentials (only the FTP ones, for the fetch
   scripts) live in a gitignored `.env` (copy `.env.example`). Never print, log or
   paste a secret value.
4. **Run tests in a container, not on your host** (command below).
5. **Don't delete files to tidy up.** Move them into the gitignored `local/` folder.
6. **Every PR description says:** what changed and why; where it was tested; and
   whether it changes the input grid contract (`grilla.py`) or the output format.

## Design decisions not to break (details in README, "Decisiones de diseño")
- **The gate never loses a system.** It must read `cfg.echotop_dbz` and
  `cfg.echotop_min_above_h0_m`, **never its own constants**, and crop to
  `cfg.r_max_km` **before** taking the maximum. `test_gate.py` checks this.
- **No degraded mode.** `gate.resolver_h0()` always returns a number and falls back
  to climatology. Never let H0 become `None`/`NaN`: C2 would silently pass everything.
- **State between volumes** (`estado.py`): a volume discarded by the gate sets
  `previous = []`; a gap longer than `max_gap_tracking_min` restarts tracking.
- **`grilla.py` is the only module that knows the input format.** A format change
  stays inside `grilla.leer()`.

## Conventions that were once recorded wrong
- **`referencia_z` is `"radar"`** (height above the antenna), not `"msnm"`. Geometry
  files have `radar_altitude=0`; the true antenna altitude comes from `radar.altitude`.
- `scripts/grid_writer.py` flips rows (radarlib row 0 = south → tracker row 0 =
  north), so `Config.invertir_y = False`. Flipping in both places mirrors the grid
  silently.

## Detection thresholds
Tuned for RMA1 (CAPPI at 2500 m with the melting layer masked, `min_area_km2=50`,
`dbz_threshold=35`). Scattered convection on other radars may yield few or no tracks;
that is physics, not a bug. **Don't change thresholds without the met team.**

## How to test (in a container)
```bash
docker build -t gl-tracker-tests .
docker run --rm -v "$PWD":/workspace/gl-tracker:ro -w /workspace/gl-tracker \
  -e PYTHONPATH=/workspace/gl-tracker gl-tracker-tests python test_gate.py
# expected: "test_gate: OK"
```
The first build takes a few minutes (GDAL is compiled for Python).

## Language
Keep each file in its existing language: README, SETUP and code comments are in
Spanish. New agent docs are in English.
