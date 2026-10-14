#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Coverage report over the grids the tracker will see.

On a long span the per-volume log lines scroll away, and a volume that silently
failed to download or to grid is invisible until the tracking looks wrong. A
missing volume matters more than it appears: it widens the gap between its
neighbours, and once that gap passes Config.max_gap_tracking_min the tracker
discards its previous frame and every system restarts as a new track.

    python status.py [--grids DIR] [--max-gap MIN] [--expect TS,TS,...]
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

TS_RE = re.compile(r"(\d{8}T\d{6})")


def stamps_in(d: Path, radar: str | None = None) -> list[datetime]:
    pattern = f"{radar}_*.nc" if radar else "*.nc"
    out = []
    for p in sorted(d.glob(pattern)):
        m = TS_RE.search(p.name)
        if m:
            out.append(datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
                       .replace(tzinfo=timezone.utc))
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grids", type=Path, default=Path("/workspace/grids"))
    ap.add_argument("--radar-data", type=Path, default=Path("/workspace/radar_data"))
    ap.add_argument("--radar", default=None,
                    help="filter by radar name (e.g. RMA5) when all radars share a directory")
    ap.add_argument("--max-gap", type=float, default=20.0,
                    help="Config.max_gap_tracking_min (default 20)")
    ap.add_argument("--expect", default=None,
                    help="comma-separated timestamps that were requested")
    args = ap.parse_args()

    radar = args.radar.upper() if args.radar else None
    grids = stamps_in(args.grids, radar)
    fetched = stamps_in(args.radar_data, radar)

    if not grids:
        print("no grids found — nothing for the tracker to read")
        return 1

    print(f"grids    : {len(grids)}  {grids[0]:%Y-%m-%d %H:%M:%S} -> {grids[-1]:%H:%M:%S} UTC")
    print(f"CFRadial : {len(fetched)} in {args.radar_data}")

    if args.expect:
        want = sorted(datetime.strptime(t.strip().rstrip("Z"), "%Y%m%dT%H%M%S")
                      .replace(tzinfo=timezone.utc)
                      for t in args.expect.split(",") if t.strip())
        have_g, have_f = set(grids), set(fetched)
        missing = [t for t in want if t not in have_g]
        if missing:
            print(f"\nmissing  : {len(missing)} of {len(want)} requested")
            for t in missing:
                why = ("fetched but failed to grid (incomplete scan?)"
                       if t in have_f else "never fetched (FTP failure?)")
                print(f"   {t:%H:%M:%S}  {why}")
        else:
            print(f"\nmissing  : none — all {len(want)} requested volumes gridded")

    # Cadence, and gaps wide enough to reset the tracking state.
    if len(grids) > 1:
        deltas = [(b - a).total_seconds() / 60.0 for a, b in zip(grids, grids[1:])]
        typical = sorted(deltas)[len(deltas) // 2]
        print(f"\ncadence  : {typical:.1f} min typical")
        breaks = [(a, b, d) for a, b, d in zip(grids, grids[1:], deltas) if d > args.max_gap]
        if breaks:
            print(f"gaps     : {len(breaks)} exceed max_gap_tracking_min={args.max_gap:.0f}"
                  f" — tracking restarts after each")
            for a, b, d in breaks:
                print(f"   {a:%H:%M:%S} -> {b:%H:%M:%S}   {d:.1f} min")
        else:
            print(f"gaps     : none exceed {args.max_gap:.0f} min — tracking runs unbroken")

    return 0


if __name__ == "__main__":
    sys.exit(main())
