#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""List volume timestamps available on the radar FTP, one per line.

Feeds pipeline.sh so a time span can be given as a range instead of a hand-typed
list of timestamps.

    python list_volumes.py RMA5 --date 2026/10/13 --from 14:00 --to 18:00
    python list_volumes.py RMA5 --latest 12
    python list_volumes.py RMA5 --date 2026/10/12 --sizes

Only volume 01 is listed (the one carrying DBZH and RHOHV). Volumes missing
RHOHV are skipped unless --allow-no-rhohv is given, because grilla.leer()
rejects them downstream anyway.
"""

from __future__ import annotations

import argparse
import ftplib
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HOST = os.environ.get("FTP_HOST", "")
USER = os.environ.get("FTP_USER", "")
PASS = os.environ.get("FTP_PASS", "")

TS_RE = re.compile(r"(\d{8}T\d{6}Z)")


class Ftp:
    """FTP wrapper that reconnects: long directory walks get dropped often."""

    def __init__(self, timeout: int = 25):
        self.timeout = timeout
        self.conn: ftplib.FTP | None = None
        self._connect()

    def _connect(self) -> None:
        self.conn = ftplib.FTP(HOST, timeout=self.timeout)
        self.conn.login(USER, PASS)

    def _retry(self, fn, default):
        for attempt in (1, 2, 3):
            try:
                return fn()
            except ftplib.error_perm:
                return default          # directory simply does not exist
            except (ftplib.error_temp, ftplib.error_proto, OSError, EOFError):
                if attempt == 3:
                    return default
                try:
                    self.conn.close()
                except Exception:
                    pass
                try:
                    self._connect()
                except Exception:
                    return default
        return default

    def ls(self, path: str) -> list[str]:
        return self._retry(
            lambda: [x.split("/")[-1] for x in self.conn.nlst(path)], [])

    def ls_sized(self, path: str) -> list[tuple[str, int]]:
        """Names and byte sizes from one LIST, instead of NLST plus a SIZE each.

        Scanning a whole day one SIZE at a time is hundreds of round trips, and
        the server drops the control connection long before it finishes.
        """
        def run():
            lines: list[str] = []
            self.conn.dir(path, lines.append)
            out = []
            for ln in lines:
                parts = ln.split(maxsplit=8)
                if len(parts) < 9 or ln.startswith("d"):
                    continue
                try:
                    out.append((parts[8].strip(), int(parts[4])))
                except ValueError:
                    continue
            return out
        return self._retry(run, [])

    def close(self) -> None:
        try:
            self.conn.quit()
        except Exception:
            pass


def _hhmm(s: str) -> tuple[int, int]:
    s = s.strip()
    if ":" in s:
        h, m = s.split(":", 1)
    else:
        h, m = s[:2], s[2:] or "0"
    return int(h), int(m)


def volumes_for_date(ftp: Ftp, radar: str, date: str,
                     allow_no_rhohv: bool,
                     hours: set[str] | None = None) -> list[tuple[str, int]]:
    """Volume-01 timestamps (and DBZH sizes) for one day.

    `hours` restricts the walk to the hour directories actually wanted; scanning
    all 24 when only a window was asked for is the difference between a quick
    listing and a dropped connection.
    """
    base = f"/L2/{radar}/{date}"
    out: list[tuple[str, int]] = []
    for hour in sorted(h for h in ftp.ls(base) if re.fullmatch(r"\d{2}", h)):
        if hours is not None and hour not in hours:
            continue
        for minute in sorted(ftp.ls(f"{base}/{hour}")):
            entries = ftp.ls_sized(f"{base}/{hour}/{minute}")
            dbzh = [(n, sz) for n, sz in entries if "_01_DBZH_" in n]
            if not dbzh:
                continue
            if not allow_no_rhohv and not any("_01_RHOHV_" in n for n, _ in entries):
                continue
            m = TS_RE.search(dbzh[0][0])
            if m:
                out.append((m.group(1), dbzh[0][1]))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("radar")
    p.add_argument("--date", default=None, help="YYYY/MM/DD (default: today UTC)")
    p.add_argument("--from", dest="t_from", default=None, help="start time HH:MM")
    p.add_argument("--to", dest="t_to", default=None, help="end time HH:MM")
    p.add_argument("--latest", type=int, default=None,
                   help="the N most recent volumes, looking back a day if needed")
    p.add_argument("--every", type=int, default=1,
                   help="keep every Nth volume (thin a long span)")
    p.add_argument("--max", type=int, default=None, help="cap the number returned")
    p.add_argument("--sizes", action="store_true",
                   help="also print the DBZH size in MB (a proxy for echo content)")
    p.add_argument("--allow-no-rhohv", action="store_true")
    args = p.parse_args()

    radar = args.radar.upper()
    try:
        ftp = Ftp()
    except Exception as e:
        sys.exit(f"ERROR: cannot reach FTP {HOST}: {e}")

    today = datetime.now(timezone.utc)
    rows: list[tuple[str, int]] = []

    if args.latest:
        # Walk back day by day until enough volumes are found.
        for back in range(0, 3):
            day = (today - timedelta(days=back)).strftime("%Y/%m/%d")
            rows = volumes_for_date(ftp, radar, day,
                                    args.allow_no_rhohv) + rows
            if len(rows) >= args.latest:
                break
        rows = rows[-args.latest:]
    else:
        date = args.date or today.strftime("%Y/%m/%d")
        hours = None
        if args.t_from or args.t_to:
            h_lo = _hhmm(args.t_from)[0] if args.t_from else 0
            h_hi = _hhmm(args.t_to)[0] if args.t_to else 23
            hours = {f"{h:02d}" for h in range(h_lo, h_hi + 1)}
        rows = volumes_for_date(ftp, radar, date, args.allow_no_rhohv, hours)
        if args.t_from or args.t_to:
            lo = _hhmm(args.t_from) if args.t_from else (0, 0)
            hi = _hhmm(args.t_to) if args.t_to else (23, 59)
            def keep(ts: str) -> bool:
                t = datetime.strptime(ts, "%Y%m%dT%H%M%SZ")
                return lo <= (t.hour, t.minute) <= hi
            rows = [r for r in rows if keep(r[0])]

    ftp.close()

    if args.every > 1:
        rows = rows[::args.every]
    if args.max:
        rows = rows[:args.max]

    for ts, size in rows:
        print(f"{ts}\t{size/1e6:.2f}" if args.sizes else ts)

    if not rows:
        print("no volumes matched", file=sys.stderr)
        return 1
    print(f"{len(rows)} volume(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
