#!/usr/bin/env bash
# Drive the whole chain for one radar, from the host.
#
#   BUFR (FTP) -> CFRadial .nc -> Cartesian grid .nc -> SpatiaLite
#
# Everything runs inside the gl-tracker image; nothing but docker is needed on
# the host. Run `pipeline.sh --help` for usage.

set -euo pipefail

IMAGE="${GL_TRACKER_IMAGE:-gl-tracker:dev}"
HOME_DIR="${HOME}"
TRACKER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RADARLIB_DIR="${RADARLIB_DIR:-${HOME_DIR}/gl-radarlib}"
GEOMETRY_DIR="${GEOMETRY_DIR:-${HOME_DIR}/geometry}"
RADAR_DATA_DIR="${RADAR_DATA_DIR:-${HOME_DIR}/radar_data}"
GRIDS_DIR="${GRIDS_DIR:-${HOME_DIR}/grids}"
TRACKING_OUT_DIR="${TRACKING_OUT_DIR:-${HOME_DIR}/tracking_out}"

usage() {
cat <<'USAGE'
Usage:
  pipeline.sh survey RADAR [DATE] [--from HH:MM --to HH:MM]
                                         What volumes exist, and how big
  pipeline.sh fetch   RADAR TS [TS ...]  Download volumes -> CFRadial NetCDF
  pipeline.sh fetch   RADAR --latest N   ... or the N most recent volumes
  pipeline.sh fetch   RADAR --date D --from HH:MM --to HH:MM
  pipeline.sh list    RADAR [flags]      Just print matching timestamps
  pipeline.sh grid    RADAR              Grid every CFRadial for RADAR
  pipeline.sh track                      Run the tracker over all grids
  pipeline.sh db                         Show what landed in the database
  pipeline.sh map                        Render the results as an interactive map
  pipeline.sh open                       Open the map (Windows browser, via WSL)
  pipeline.sh serve [PORT]               Serve the map on localhost (default 8900)
  pipeline.sh status                     Coverage: missing volumes and tracking gaps
  pipeline.sh clean                      Empty grids/ and tracking_out/
  pipeline.sh run     RADAR TS [TS ...]  fetch + grid + track + map
  pipeline.sh run     RADAR --latest N   ... over the N most recent volumes

RADAR  e.g. RMA4, RMA5, RMA8, AR8   (a geometry must exist in ~/geometry/RADAR/)
DATE   YYYY/MM/DD, defaults to today
TS     volume timestamp, e.g. 20261013T182034Z

Examples:
  ./scripts/pipeline.sh survey RMA5
  ./scripts/pipeline.sh run RMA5 --latest 12
  ./scripts/pipeline.sh run RMA5 --date 2026/10/13 --from 14:00 --to 18:00
  ./scripts/pipeline.sh list RMA5 --date 2026/10/13 --from 12:00 --to 20:00 --sizes
  ./scripts/pipeline.sh run RMA5 20261013T175331Z 20261013T180232Z
  ./scripts/pipeline.sh db

Host directories used (created on demand):
  ~/radar_data    CFRadial NetCDFs
  ~/grids         Cartesian grids  (tracker input)
  ~/tracking_out  systems-db.sqlite + estado_tracking.pkl
  ~/geometry      interpolation geometries (read-only)
USAGE
}

mkdir -p "${RADAR_DATA_DIR}" "${GRIDS_DIR}" "${TRACKING_OUT_DIR}"

dock() {
  # -i so the heredoc-driven subcommands can pipe a script in on stdin
  docker run --rm -i \
    -v "${TRACKER_DIR}:/workspace/gl-tracker" \
    -v "${RADARLIB_DIR}:/workspace/gl-radarlib" \
    -v "${GEOMETRY_DIR}:/workspace/geometry:ro" \
    -v "${RADAR_DATA_DIR}:/workspace/radar_data" \
    -v "${GRIDS_DIR}:/workspace/grids" \
    -v "${TRACKING_OUT_DIR}:/workspace/tracking_out" \
    -e PYTHONPATH=/workspace/gl-tracker:/workspace/gl-tracker/scripts:/workspace/gl-radarlib/src \
    -e GRILLA_RAIZ=/workspace/grids \
    -e TRACKING_OUT=/workspace/tracking_out \
    -e FTP_HOST="${FTP_HOST:-}" \
    -e FTP_USER="${FTP_USER:-}" \
    -e FTP_PASS="${FTP_PASS:-}" \
    -w /workspace/gl-tracker \
    "${IMAGE}" "$@"
}

# Py-ART and GDAL print a banner and a FutureWarning on every import.
quiet() { grep -vE "FutureWarning|warnings\.warn|^##|^###|Py-ART|ARM |Helmus|Department of Energy|Atmospheric Radiation|publication, please|library for working|open source|partly supported|Grupo Radar|font_manager|^$" || true; }

# Turn either literal timestamps or range flags into a timestamp list.
# Range flags are passed straight through to list_volumes.py.
resolve_ts() {
  local radar="$1"; shift
  case "${1:-}" in
    --*) dock python scripts/list_volumes.py "${radar}" "$@" 2>&1 | tee /dev/stderr | grep -E "^[0-9]{8}T" | cut -f1 ;;
    *)   printf '%s\n' "$@" ;;
  esac
}

cmd="${1:-}"; shift || true

case "${cmd}" in

list)
  RADAR="${1:?radar required}"; shift
  dock python scripts/list_volumes.py "${RADAR}" "$@"
  ;;

survey)
  RADAR="${1:?radar required}"; shift
  DATE="${1:-}"; [ -n "${DATE}" ] && shift || true
  # Delegates the FTP walk to list_volumes.py: one LIST per directory and a
  # reconnecting retry, rather than an NLST plus a SIZE for every volume.
  dock python scripts/list_volumes.py "${RADAR}" \
      ${DATE:+--date "${DATE}"} --sizes "$@" \
    2> >(grep -v "^[0-9]\+ volume" >&2) \
    | awk -v radar="${RADAR}" -v date="${DATE:-today}" '
      BEGIN{ printf "%s  %s\n\n%-18s %10s   %s\n", radar, date,
                    "timestamp", "DBZH size", "(bigger = more echo)" }
      { ts=$1; mb=$2+0; n=int(mb*7); bar="";
        for(i=0;i<n && i<48;i++) bar=bar "#";
        printf "%-18s %7.2f MB  %s\n", ts, mb, bar;
        if (mb>best){best=mb} a[NR]=ts; b[NR]=mb }
      END{ if(NR==0){ print "no volumes found"; exit }
           printf "\nLargest (likely most convective):\n  ";
           for(k=0;k<8;k++){ bi=0; bv=-1;
             for(i=1;i<=NR;i++) if(b[i]>bv){bv=b[i]; bi=i}
             if(bi){ printf "%s ", a[bi]; b[bi]=-1 } }
           printf "\n" }'
  ;;

fetch)
  RADAR="${1:?radar required}"; shift
  [ "$#" -gt 0 ] || { echo "need timestamps, or a range (--latest N / --from HH:MM --to HH:MM)" >&2; exit 1; }
  TSLIST="$(resolve_ts "${RADAR}" "$@")"
  [ -n "${TSLIST}" ] || { echo "no volumes matched" >&2; exit 1; }
  echo "--- $(echo "${TSLIST}" | wc -l) volume(s) to fetch"
  for TS in ${TSLIST}; do
    echo "--- fetch ${RADAR} ${TS}"
    dock python scripts/fetch_radar_volume.py "${RADAR}" \
      --timestamp "${TS}" --fields DBZH RHOHV \
      --out-dir /workspace/radar_data \
      --radarlib-src /workspace/gl-radarlib/src 2>&1 | quiet | grep -E "Written|ERROR|FAILED|ready" || true
  done
  ;;

grid)
  RADAR="${1:?radar required}"; shift
  if [ "$#" -gt 0 ]; then
    # Specific timestamps supplied: build one glob per timestamp so we don't
    # re-grid the entire archive when only a window was requested.
    FILES=""
    for TS in "$@"; do
      FILES="${FILES} /workspace/radar_data/${RADAR}_*${TS}*.nc"
    done
    dock sh -c "python scripts/make_grid.py ${FILES}" \
      2>&1 | quiet | grep -E "coverage|wrote|ERROR|grids written|failed" || true
  else
    # No timestamps: grid everything for this radar (skips already-done files).
    dock sh -c "python scripts/make_grid.py /workspace/radar_data/${RADAR}_*.nc" \
      2>&1 | quiet | grep -E "coverage|wrote|ERROR|grids written|failed" || true
  fi
  ;;

track)
  # Grids must be >30s old (Config.grilla_edad_minima) to count as closed.
  dock python operativo.py --una-vez --desde-cero 2>&1 | quiet
  ;;

db)
  dock python - <<'PY' | quiet
import sqlite3, pathlib
p = pathlib.Path("/workspace/tracking_out/systems-db.sqlite")
if not p.exists():
    print("no database yet — run `pipeline.sh track` first"); raise SystemExit
con = sqlite3.connect(p); con.enable_load_extension(True)
con.load_extension("mod_spatialite"); cur = con.cursor()
n = cur.execute("select count(*) from systems").fetchone()[0]
print(f"systems: {n} row(s)")
if n:
    print(f"\n{'date_time':<26} {'conv':>4} {'etop_m':>7} {'dist_km':>8} {'maxdBZ':>7} {'core_km2':>9} {'rhohv':>6}  event")
    for dt, c, et, di, mx, ca, rh, ev in cur.execute(
        "select date_time,is_convective,echo_top_max_m,dist_centroid_km,max,"
        "core_area_km2,core_rhohv_mean,event from systems order by date_time"):
        g = lambda v, n=1: "--" if v is None else (f"{v:.{n}f}" if isinstance(v, float) else str(v))
        print(f"{str(dt):<26} {g(c,0):>4} {g(et,0):>7} {g(di):>8} {g(mx):>7} {g(ca):>9} {g(rh,2):>6}  {ev}")
for h in (9, 18, 27, 36, 45, 54, 63):
    try: print(f"forecast_{h}min: {cur.execute(f'select count(*) from forecast_{h}min').fetchone()[0]}")
    except Exception: pass
con.close()
PY
  ;;

map)
  dock python scripts/export_map.py \
    --db /workspace/tracking_out/systems-db.sqlite \
    --grids /workspace/grids \
    --out /workspace/tracking_out/tracking_map.html 2>&1 | quiet
  echo
  echo "view it with:  ./scripts/pipeline.sh open      (or: serve)"
  ;;

open)
  # WSL has no browser of its own; explorer.exe hands the file to the Windows
  # default browser. Falls back to the usual openers elsewhere.
  MAP="${TRACKING_OUT_DIR}/tracking_map.html"
  [ -f "${MAP}" ] || { echo "no map yet — run \`pipeline.sh map\` first" >&2; exit 1; }
  if command -v wslpath >/dev/null && command -v explorer.exe >/dev/null; then
    explorer.exe "$(wslpath -w "${MAP}")" || true    # explorer.exe exits 1 even on success
  elif command -v xdg-open >/dev/null; then xdg-open "${MAP}"
  elif command -v open     >/dev/null; then open "${MAP}"
  else echo "open it manually: ${MAP}"; fi
  ;;

serve)
  # Most reliable route in WSL2: WSL localhost is forwarded to Windows, so the
  # Windows browser reaches this directly, with no UNC-path handling involved.
  PORT="${1:-8900}"
  MAP="${TRACKING_OUT_DIR}/tracking_map.html"
  [ -f "${MAP}" ] || { echo "no map yet — run \`pipeline.sh map\` first" >&2; exit 1; }
  echo "serving ${TRACKING_OUT_DIR} on http://localhost:${PORT}"
  echo "open:  http://localhost:${PORT}/tracking_map.html"
  echo "(ctrl-c to stop)"
  cd "${TRACKING_OUT_DIR}" && exec python3 -m http.server "${PORT}" --bind 127.0.0.1
  ;;

status)
  RADAR="${1:-}"; shift 2>/dev/null || true
  dock python scripts/status.py --grids /workspace/grids \
       --radar-data /workspace/radar_data \
       ${RADAR:+--radar "${RADAR}"} \
       ${EXPECT:+--expect "${EXPECT}"} 2>&1 | quiet
  ;;

clean)
  rm -f "${GRIDS_DIR}"/*.nc "${TRACKING_OUT_DIR}/systems-db.sqlite" \
        "${TRACKING_OUT_DIR}/estado_tracking.pkl" 2>/dev/null || true
  echo "cleared grids and tracking_out (CFRadial files in radar_data kept)"
  ;;

run)
  RADAR="${1:?radar required}"; shift
  TSLIST="$(resolve_ts "${RADAR}" "$@")"
  [ -n "${TSLIST}" ] || { echo "no volumes matched" >&2; exit 1; }
  "${BASH_SOURCE[0]}" fetch "${RADAR}" ${TSLIST}
  "${BASH_SOURCE[0]}" grid "${RADAR}" ${TSLIST}
  echo "--- waiting 31s: the tracker ignores grids newer than grilla_edad_minima"
  sleep 31
  "${BASH_SOURCE[0]}" track
  "${BASH_SOURCE[0]}" db
  "${BASH_SOURCE[0]}" map
  echo
  EXPECT="$(echo ${TSLIST} | tr ' ' ',')" "${BASH_SOURCE[0]}" status "${RADAR}"
  ;;

-h|--help|help|"") usage ;;
*) echo "unknown command: ${cmd}" >&2; usage; exit 1 ;;
esac
