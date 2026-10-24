#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch a complete radar volume from FTP, decode BUFR files, save as PyART NetCDF.

The output NetCDF is ready to be fed directly to test_tracking_pipeline.py.

Usage
-----
  python fetch_radar_volume.py RADAR [options]

  RADAR   Radar identifier (e.g., RMA5, RMA1, RMA4)

Options
  --strategy STR     Scan strategy code (default: 0315)
  --vol-nr STR       Volume number (default: 01)
  --fields F [F...]  BUFR field names to download.
                     Default: DBZH RHOHV ZDR PHIDP KDP
                     Minimum required by tracker: DBZH RHOHV
  --timestamp TS     Target exact volume timestamp YYYYMMDDTHHMMSSZ.
                     If omitted, the latest available volume is fetched.
  --out-dir PATH     Directory for downloads and output NetCDF.
                     Default: /tmp/radar_fetch/
  --radarlib-src P   radarlib src/ dir to add to sys.path (if not installed).
                     Default: ~/gl-radarlib/src
  --ftp-host HOST    FTP host     (default: 200.16.116.24)
  --ftp-user USER    FTP user     (default: env FTP_USER)
  --ftp-pass PASS    FTP password (default: env FTP_PASS)
  --keep-bufr        Keep downloaded BUFR files after creating the NetCDF.
  --dry-run          List the BUFR files that would be downloaded and exit.

Output
------
  <out-dir>/bufr/   (optionally removed with --keep-bufr off)
  <out-dir>/<RADAR>_<strategy>_<vol_nr>_<timestamp>.nc

Example
-------
  # Latest RMA5 volume:
  python fetch_radar_volume.py RMA5 --out-dir /tmp/rma5

  # Specific timestamp:
  python fetch_radar_volume.py RMA5 --timestamp 20260401T205000Z --out-dir /tmp/rma5

  # Minimal fields only (faster download):
  python fetch_radar_volume.py RMA5 --fields DBZH RHOHV
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetch_radar_volume")

# FTP credentials must be supplied via env vars FTP_HOST / FTP_USER / FTP_PASS
# (or --ftp-host / --ftp-user / --ftp-pass on the command line).
_DEFAULT_FTP_HOST = os.environ.get("FTP_HOST", "")
_DEFAULT_FTP_USER = os.environ.get("FTP_USER", "")
_DEFAULT_FTP_PASS = os.environ.get("FTP_PASS", "")
_DEFAULT_FIELDS = ["DBZH", "RHOHV", "ZDR", "PHIDP", "KDP"]


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("radar", metavar="RADAR", help="Radar identifier (e.g. RMA5)")
    p.add_argument("--strategy", default="0315")
    p.add_argument("--vol-nr", default="01", dest="vol_nr")
    p.add_argument("--fields", nargs="+", default=_DEFAULT_FIELDS,
                   metavar="FIELD", help="BUFR fields to download")
    p.add_argument("--timestamp", default=None,
                   help="Target timestamp YYYYMMDDTHHMMSSZ (default: latest)")
    p.add_argument("--out-dir", type=Path, default=Path("/tmp/radar_fetch"),
                   dest="out_dir")
    p.add_argument("--radarlib-src", type=Path,
                   default=Path.home() / "gl-radarlib" / "src",
                   dest="radarlib_src")
    p.add_argument("--ftp-host", default=_DEFAULT_FTP_HOST, dest="ftp_host")
    p.add_argument("--ftp-user", default=_DEFAULT_FTP_USER, dest="ftp_user")
    p.add_argument("--ftp-pass", default=_DEFAULT_FTP_PASS, dest="ftp_pass")
    p.add_argument("--keep-bufr", action="store_true", dest="keep_bufr")
    p.add_argument("--force", action="store_true",
                   help="refetch even if the output NetCDF already exists")
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    return p.parse_args()


def _setup_radarlib(radarlib_src: Path) -> None:
    src = radarlib_src.expanduser().resolve()
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
        logger.info(f"Added radarlib src to sys.path: {src}")
    try:
        import radarlib  # noqa: F401
    except ImportError as e:
        sys.exit(
            f"ERROR: Could not import radarlib: {e}\n"
            f"  Install radarlib or pass --radarlib-src."
        )


def _ftp_path_from_timestamp(radar: str, ts: datetime, base_dir: str = "L2") -> str:
    """Build the FTP minute-directory path for a given timestamp."""
    mmss = f"{ts.minute:02d}{ts.second:02d}"
    return (
        f"/{base_dir}/{radar}"
        f"/{ts.year:04d}/{ts.month:02d}/{ts.day:02d}"
        f"/{ts.hour:02d}/{mmss}"
    )


def _parse_timestamp(ts_str: str) -> datetime:
    fmt = "%Y%m%dT%H%M%SZ" if ts_str.endswith("Z") else "%Y%m%dT%H%M%S"
    return datetime.strptime(ts_str.rstrip("Z"), fmt.rstrip("Z")).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# FTP: find and download BUFR files for one volume
# ---------------------------------------------------------------------------

def find_and_download_bufr(
    ftp_host: str,
    ftp_user: str,
    ftp_pass: str,
    radar: str,
    strategy: str,
    vol_nr: str,
    fields: list[str],
    target_ts: datetime | None,
    out_dir: Path,
    dry_run: bool = False,
) -> list[Path]:
    """Download one complete volume from FTP. Returns list of local BUFR paths."""
    from radarlib.io.ftp.ftp_client import RadarFTPClient
    from radarlib.io.bufr.bufr import BUFRFilename

    bufr_dir = out_dir / "bufr"
    bufr_dir.mkdir(parents=True, exist_ok=True)

    with RadarFTPClient(ftp_host, ftp_user, ftp_pass) as ftp:

        # ── Step 1: Locate the minute directory ──────────────────────────────
        if target_ts is not None:
            # User specified an exact timestamp → construct path directly
            minute_path = _ftp_path_from_timestamp(radar, target_ts)
            logger.info(f"Using specified timestamp: {minute_path}")
            try:
                all_files = ftp.list_dir(minute_path)
            except Exception as e:
                sys.exit(f"ERROR: Could not list FTP directory {minute_path}: {e}")
        else:
            # Find the latest DBZH file as anchor
            logger.info(f"Searching for latest {radar} vol {vol_nr} on FTP...")
            ref = ftp.find_last_bufr_file(
                radar=radar,
                strategy=strategy,
                field="DBZH",
            )
            if ref is None:
                sys.exit(f"ERROR: No BUFR files found for {radar} on the FTP server.")

            # Verify that the found file matches the requested vol_nr
            parsed = BUFRFilename(ref.filename)
            if parsed.volume != vol_nr:
                logger.warning(
                    f"Found vol {parsed.volume} (expected {vol_nr}); "
                    f"proceeding with {parsed.volume}."
                )

            target_ts = parsed.datetime
            minute_path = ref.remote_path
            logger.info(f"Found latest: {ref.filename}")

            try:
                all_files = ftp.list_dir(minute_path)
            except Exception as e:
                sys.exit(f"ERROR: Could not list {minute_path}: {e}")

        logger.info(f"Minute directory: {minute_path} ({len(all_files)} file(s))")

        # ── Step 2: Filter to requested fields for this volume ───────────────
        ts_str = target_ts.strftime("%Y%m%dT%H%M%SZ")
        to_download: list[tuple[str, str]] = []  # (remote_path, local_filename)

        missing_fields = []
        for field in fields:
            fname = f"{radar}_{strategy}_{vol_nr}_{field}_{ts_str}.BUFR"
            if fname in all_files:
                remote = f"{minute_path}/{fname}"
                to_download.append((remote, fname))
            else:
                # Try case-insensitive match
                match = next((f for f in all_files
                              if f.upper() == fname.upper()), None)
                if match:
                    to_download.append((f"{minute_path}/{match}", match))
                else:
                    missing_fields.append(field)

        if missing_fields:
            logger.warning(f"Fields not found on FTP: {missing_fields}")

        required = {"DBZH", "RHOHV"}
        found_fields = {BUFRFilename(fname).field for _, fname in to_download}
        if not required.issubset(found_fields):
            missing_req = required - found_fields
            sys.exit(
                f"ERROR: Required fields {missing_req} not available for this volume.\n"
                f"  Available: {found_fields}"
            )

        if dry_run:
            print("\nDry run — files that would be downloaded:")
            for remote, fname in to_download:
                print(f"  {remote}")
            return []

        # ── Step 3: Download ─────────────────────────────────────────────────
        logger.info(f"Downloading {len(to_download)} BUFR file(s)...")
        local_paths = []
        for remote, fname in to_download:
            local = bufr_dir / fname
            if local.exists() and local.stat().st_size > 0:
                logger.info(f"  Already exists: {fname}")
            else:
                # The FTP drops long transfers fairly often; one retry turns most
                # of those into a success instead of a half-built volume.
                for attempt in (1, 2, 3):
                    logger.info(f"  Downloading: {fname}"
                                + (f" (attempt {attempt})" if attempt > 1 else ""))
                    try:
                        ftp.download_file(remote, local)
                        break
                    except Exception as e:
                        logger.error(f"  FAILED: {fname}: {e}")
                        local.unlink(missing_ok=True)
                else:
                    continue
            local_paths.append(local)

        # A volume missing DBZH or RHOHV is useless downstream: grilla.leer()
        # raises on absent RHOHV. Fail here rather than writing a NetCDF that
        # only looks complete.
        got = {BUFRFilename(p.name).field for p in local_paths}
        if not required.issubset(got):
            sys.exit(
                f"ERROR: required field(s) {sorted(required - got)} failed to "
                f"download for {ts_str}; refusing to write a partial volume."
            )

    logger.info(f"Downloaded {len(local_paths)} file(s) to {bufr_dir}")
    return local_paths


# ---------------------------------------------------------------------------
# BUFR → NetCDF
# ---------------------------------------------------------------------------

def decode_and_save_netcdf(
    bufr_paths: list[Path],
    out_dir: Path,
    keep_bufr: bool,
) -> Path:
    """Decode BUFR files and write one CFRadial NetCDF. Returns the NetCDF path."""
    import gc
    import pyart
    from radarlib.io.bufr.bufr import bufr_to_dict
    from radarlib.io.bufr.pyart_writer import bufr_fields_to_pyart_radar
    from radarlib.utils.names_utils import get_netcdf_filename_from_bufr_filename

    logger.info(f"Decoding {len(bufr_paths)} BUFR file(s)...")
    decoded_fields = []

    for bufr_path in bufr_paths:
        size = bufr_path.stat().st_size
        if size == 0:
            logger.error(f"  Skipping 0-byte file (corrupt): {bufr_path.name}")
            continue
        with open(bufr_path, "rb") as fh:
            magic = fh.read(4)
        if magic != b"BUFR":
            logger.error(f"  Skipping invalid BUFR (magic={magic!r}): {bufr_path.name}")
            continue

        logger.info(f"  Decoding: {bufr_path.name}")
        try:
            decoded = bufr_to_dict(str(bufr_path))
        except Exception as e:
            logger.error(f"  Decode failed for {bufr_path.name}: {e}")
            continue

        if decoded is None:
            logger.warning(f"  bufr_to_dict returned None for {bufr_path.name}")
            continue
        decoded_fields.append(decoded)

    if not decoded_fields:
        sys.exit("ERROR: No BUFR files could be decoded.")

    logger.info(f"Decoded {len(decoded_fields)} field(s). Building PyART radar object...")
    try:
        radar = bufr_fields_to_pyart_radar(decoded_fields)
    except Exception as e:
        sys.exit(f"ERROR: Failed to build PyART radar: {e}")
    finally:
        del decoded_fields
        gc.collect()

    if radar is None:
        sys.exit("ERROR: bufr_fields_to_pyart_radar returned None.")

    # Derive output filename from the first BUFR file
    netcdf_name = get_netcdf_filename_from_bufr_filename(bufr_paths[0].name)
    out_dir.mkdir(parents=True, exist_ok=True)
    netcdf_path = out_dir / netcdf_name

    logger.info(f"Saving NetCDF: {netcdf_path}")
    try:
        pyart.io.write_cfradial(str(netcdf_path), radar)
    except Exception as e:
        sys.exit(f"ERROR: Failed to write NetCDF: {e}")
    finally:
        del radar
        gc.collect()

    logger.info(f"  Written: {netcdf_path.stat().st_size / 1e6:.1f} MB")

    if not keep_bufr:
        for p in bufr_paths:
            try:
                p.unlink()
            except Exception:
                pass
        try:
            (out_dir / "bufr").rmdir()
        except Exception:
            pass  # directory not empty — that's fine
        logger.info("BUFR files removed (pass --keep-bufr to retain them).")

    return netcdf_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = _parse_args()
    _setup_radarlib(args.radarlib_src)

    radar = args.radar.upper()
    out_dir = args.out_dir.expanduser().resolve()
    fields = [f.upper() for f in args.fields]

    # Validate required fields
    if "DBZH" not in fields or "RHOHV" not in fields:
        logger.warning(
            "DBZH and RHOHV are both required by gl-tracker. "
            "Adding them to the field list."
        )
        for req in ("DBZH", "RHOHV"):
            if req not in fields:
                fields.insert(0, req)

    target_ts = None
    if args.timestamp:
        try:
            target_ts = _parse_timestamp(args.timestamp)
        except ValueError:
            sys.exit(f"ERROR: Invalid timestamp format '{args.timestamp}'. Use YYYYMMDDTHHMMSSZ.")

    # Skip volumes already converted, so a long span can be resumed or repaired
    # without paying for the download again.
    if target_ts is not None and not args.force:
        ts_str = target_ts.strftime("%Y%m%dT%H%M%SZ")
        existing = out_dir / f"{radar}_{args.strategy}_{args.vol_nr}_{ts_str}.nc"
        if existing.is_file() and existing.stat().st_size > 1_000_000:
            logger.info(f"Already have {existing.name} "
                        f"({existing.stat().st_size/1e6:.1f} MB) — skipping "
                        f"(use --force to refetch)")
            print(f"NetCDF ready: {existing}")
            return

    logger.info(
        f"Fetching {radar} strategy={args.strategy} vol={args.vol_nr} "
        f"fields={fields} timestamp={'latest' if target_ts is None else args.timestamp}"
    )

    bufr_paths = find_and_download_bufr(
        ftp_host=args.ftp_host,
        ftp_user=args.ftp_user,
        ftp_pass=args.ftp_pass,
        radar=radar,
        strategy=args.strategy,
        vol_nr=args.vol_nr,
        fields=fields,
        target_ts=target_ts,
        out_dir=out_dir,
        dry_run=args.dry_run,
    )

    if args.dry_run or not bufr_paths:
        return

    netcdf_path = decode_and_save_netcdf(
        bufr_paths=bufr_paths,
        out_dir=out_dir,
        keep_bufr=args.keep_bufr,
    )

    print()
    print("=" * 70)
    print(f"NetCDF ready: {netcdf_path}")
    print()
    print("Next step — run the tracking pipeline:")
    print(f"  python test_tracking_pipeline.py {netcdf_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()
