#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render the tracker's SpatiaLite output as a standalone Leaflet map.

Storm polygons and their forecast polygons are already stored in EPSG:4326, so
they go straight onto a web map with no reprojection. Each track keeps one
colour across every frame, which is what makes continuity legible: a system that
TATHU associated from one volume to the next stays the same colour as it moves.

    python export_map.py [--db PATH] [--out PATH] [--open]

Writes a single self-contained HTML file; only the basemap tiles need network.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

FORECAST_HORIZONS = (9, 18, 27, 36, 45, 54, 63)

# Validated categorical palette (see dataviz references/palette.md). Hues are
# assigned to tracks in fixed order of first appearance and never cycled; a 9th
# track folds into "other" rather than reusing a hue.
PALETTE_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                 "#e87ba4", "#008300", "#6250d6", "#e34948"]
PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500",
                "#d55181", "#008300", "#9085e9", "#e66767"]
OTHER_LIGHT, OTHER_DARK = "#898781", "#898781"

SYSTEM_COLS = [
    "name", "date_time", "max", "mean", "count", "velocity", "direction",
    "is_convective", "c2_vertical", "echo_top_max_m", "dist_centroid_km",
    "core_area_km2", "core_rhohv_mean", "h0_msnm", "event",
]


def _connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db))
    con.enable_load_extension(True)
    con.load_extension("mod_spatialite")
    return con


def _table_exists(cur, name: str) -> bool:
    return cur.execute(
        "select 1 from sqlite_master where type='table' and name=?", (name,)
    ).fetchone() is not None


def load_systems(cur) -> list[dict]:
    cols = [c[1] for c in cur.execute("PRAGMA table_info(systems)")]
    use = [c for c in SYSTEM_COLS if c in cols]
    rows = cur.execute(
        f"select {','.join(use)}, AsGeoJSON(geom) from systems "
        f"where geom is not null order by date_time"
    ).fetchall()
    out = []
    for r in rows:
        rec = dict(zip(use, r[:-1]))
        rec["geometry"] = json.loads(r[-1])
        out.append(rec)
    return out


def load_forecasts(cur) -> list[dict]:
    out = []
    for h in FORECAST_HORIZONS:
        t = f"forecast_{h}min"
        if not _table_exists(cur, t):
            continue
        for name, dt, gj in cur.execute(
            f"select name, date_time, AsGeoJSON(geom) from {t} where geom is not null"
        ):
            out.append({"name": name, "date_time": dt, "horizon": h,
                        "geometry": json.loads(gj)})
    return out


def build_overlays(grids_dir: Path, min_dbz: float = 5.0) -> dict[str, dict]:
    """COLMAX image per grid volume, keyed by the timestamp the systems use.

    The tracker rasterises with a plain equirectangular geotransform
    (grilla._build_raster: degrees per metre scaled by cos(lat0)), but Leaflet
    stretches an ImageOverlay linearly in Web Mercator pixels. Handing it the
    equirectangular array directly would bow the image against the polygons.

    Mercator leaves longitude linear and only stretches latitude, so the fix is
    a nearest-neighbour resample along rows — no reprojection library needed.
    Doing it here rather than through gdal.Warp also avoids a GDAL teardown
    abort ("double free or corruption") that fires after the file is written.
    """
    import base64, io, math, warnings
    import numpy as np
    from netCDF4 import Dataset

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from PIL import Image
        import radarlib.colormaps  # noqa: F401  — registers the grc_* colormaps
        cmap = plt.get_cmap("grc_th")      # the colormap radarlib uses for DBZH
    except Exception as e:
        print(f"  (no overlays: {type(e).__name__}: {e})", file=sys.stderr)
        return {}

    METERS_PER_DEGREE = 111320.0           # tathu.constants, matched deliberately
    VMIN, VMAX = -20.0, 70.0               # CMAP/VMIN/VMAX_REFL_NOFILTERS
    LEVELS = 48                            # ~1.9 dBZ per step

    def lat_to_merc_y(lat_deg: float) -> float:
        return math.log(math.tan(math.pi / 4.0 + math.radians(lat_deg) / 2.0))

    def merc_y_to_lat(y: float) -> float:
        return math.degrees(2.0 * math.atan(math.exp(y)) - math.pi / 2.0)

    out: dict[str, dict] = {}
    for path in sorted(grids_dir.glob("*.nc")):
        try:
            with Dataset(str(path)) as nc:
                dbzh = np.ma.filled(nc["DBZH"][:].astype("f4"), np.nan)
                x_km = nc["x_km"][:].data.astype("f8")
                y_km = nc["y_km"][:].data.astype("f8")
                lat0, lon0 = float(nc.radar_lat), float(nc.radar_lon)
                stamp = str(nc.timestamp)
        except Exception as e:
            print(f"  (skip {path.name}: {e})", file=sys.stderr)
            continue

        with warnings.catch_warnings(), np.errstate(all="ignore"):
            warnings.simplefilter("ignore", RuntimeWarning)
            colmax = np.nanmax(dbzh, axis=0)        # row 0 is already north

        valid = np.isfinite(colmax) & (colmax >= min_dbz)
        if not valid.any():
            continue

        ny, nx = colmax.shape
        x_m, y_m = x_km * 1000.0, y_km * 1000.0
        m_per_deg_lon = METERS_PER_DEGREE * math.cos(math.radians(lat0))
        dx_deg = float(x_m[1] - x_m[0]) / m_per_deg_lon
        dy_deg = float(y_m[1] - y_m[0]) / METERS_PER_DEGREE

        # Pixel-edge extent, matching _build_raster's half-pixel correction.
        west = lon0 + x_m[0] / m_per_deg_lon - dx_deg / 2.0
        north = lat0 + y_m[-1] / METERS_PER_DEGREE + dy_deg / 2.0
        east = west + nx * dx_deg
        south = north - ny * dy_deg

        # Rows evenly spaced in Mercator y, sampled from the equirectangular rows.
        y_north, y_south = lat_to_merc_y(north), lat_to_merc_y(south)
        edges = np.linspace(y_north, y_south, ny + 1)
        centres = 0.5 * (edges[:-1] + edges[1:])
        lats = np.array([merc_y_to_lat(v) for v in centres])
        src_rows = np.clip(((north - lats) / dy_deg - 0.5).round().astype(int), 0, ny - 1)

        data = np.where(valid, colmax, np.nan)[src_rows, :]
        ok = np.isfinite(data)

        # Quantise to discrete dBZ steps and store as a paletted PNG. A full
        # RGBA image of this is ~280 KB; indexed it is a fraction of that, which
        # matters because every frame is embedded in the HTML. Discrete steps
        # are also how reflectivity is conventionally displayed.
        bins = np.linspace(VMIN, VMAX, LEVELS + 1)
        idx = np.digitize(np.nan_to_num(data, nan=VMIN - 1.0), bins[1:-1], right=False) + 1
        idx = np.where(ok, np.clip(idx, 1, LEVELS), 0).astype(np.uint8)

        centres = (bins[:-1] + bins[1:]) / 2.0
        colours = (np.array(cmap((centres - VMIN) / (VMAX - VMIN)))[:, :3] * 255).astype(np.uint8)
        palette = [0, 0, 0] + [int(v) for v in colours.reshape(-1)]   # index 0 = transparent
        palette += [0] * (768 - len(palette))

        img = Image.fromarray(idx, mode="P")
        img.putpalette(palette)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True, transparency=0)
        uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

        # systems.date_time looks like '2026-10-13 14:44:20+00:00'
        key = stamp.replace("T", " ").replace("Z", "+00:00")
        out[key] = {"url": uri,
                    "bounds": [[south, west], [north, east]],
                    "kb": round(len(uri) / 1024)}
    return out


def radar_site(grids_dir: Path) -> dict | None:
    """Antenna position, read from any grid file, for a reference marker."""
    try:
        from netCDF4 import Dataset
    except ImportError:
        return None
    for p in sorted(grids_dir.glob("*.nc")):
        try:
            with Dataset(str(p)) as nc:
                return {"lat": float(nc.radar_lat), "lon": float(nc.radar_lon),
                        "alt": float(getattr(nc, "radar_alt_m", 0.0)),
                        "name": p.name.split("_")[0]}
        except Exception:
            continue
    return None


def build_html(systems, forecasts, site, overlays) -> str:
    # Every volume the tracker read becomes a frame, not only those that
    # produced systems. Otherwise a volume where nothing was detected vanishes
    # from the stepper, which makes the cadence look irregular and hides the
    # most telling case: reflectivity clearly present, no polygons drawn.
    frames = sorted(set(s["date_time"] for s in systems) | set(overlays.keys()))
    # Fixed hue order by first appearance, so a track never changes colour.
    track_order: list[str] = []
    for s in systems:
        if s["name"] not in track_order:
            track_order.append(s["name"])
    track_slot = {n: (i if i < 8 else -1) for i, n in enumerate(track_order)}

    # Tracks seen in more than one frame are the ones tracking actually linked.
    seen: dict[str, set] = {}
    for s in systems:
        seen.setdefault(s["name"], set()).add(s["date_time"])
    multi = {n for n, f in seen.items() if len(f) > 1}

    payload = {
        "systems": systems,
        "forecasts": forecasts,
        "frames": frames,
        "trackSlot": track_slot,
        "trackOrder": track_order,
        "multiFrame": sorted(multi),
        "site": site,
        "overlays": overlays,
        "paletteLight": PALETTE_LIGHT,
        "paletteDark": PALETTE_DARK,
        "otherLight": OTHER_LIGHT,
        "otherDark": OTHER_DARK,
    }

    return _TEMPLATE.replace("/*__DATA__*/", json.dumps(payload))


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>gl-tracker — convective systems</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root{
    --surface:#fcfcfb; --plane:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e;
    --muted:#898781; --hair:#e1e0d9; --axis:#c3c2b7;
  }
  html[data-theme="dark"]{
    --surface:#1a1a19; --plane:#0d0d0d; --ink:#f0efec; --ink2:#c3c2b7;
    --muted:#898781; --hair:#2c2c2a; --axis:#383835;
  }
  *{box-sizing:border-box}
  body{margin:0;height:100vh;display:flex;flex-direction:column;
    font:13px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
    background:var(--plane);color:var(--ink)}
  header{padding:12px 16px 10px;border-bottom:1px solid var(--hair);background:var(--surface)}
  h1{margin:0;font-size:15px;font-weight:600;letter-spacing:-.01em}
  .sub{color:var(--ink2);font-size:12px;margin-top:3px}
  .controls{display:flex;gap:16px;align-items:center;flex-wrap:wrap;
    padding:9px 16px;border-bottom:1px solid var(--hair);background:var(--surface)}
  .grp{display:flex;gap:7px;align-items:center}
  .lbl{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
  button{font:inherit;font-size:12px;padding:4px 10px;border:1px solid var(--axis);
    background:var(--surface);color:var(--ink);border-radius:5px;cursor:pointer}
  button:hover{border-color:var(--muted)}
  button[aria-pressed="true"]{background:var(--ink);color:var(--surface);border-color:var(--ink)}
  button:disabled{opacity:.4;cursor:default}
  #frameLabel{font-variant-numeric:tabular-nums;min-width:178px;color:var(--ink2)}
  input[type=range]{width:200px;accent-color:#2a78d6}
  #opacity{width:92px}
  main{flex:1;display:flex;min-height:0}
  #map{flex:1;background:var(--plane)}
  aside{width:336px;border-left:1px solid var(--hair);background:var(--surface);
    overflow:auto;padding:12px 14px}
  aside h2{margin:0 0 8px;font-size:11px;text-transform:uppercase;
    letter-spacing:.06em;color:var(--muted);font-weight:600}
  table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
  th{text-align:right;font-weight:500;color:var(--muted);font-size:10.5px;
    padding:3px 5px;border-bottom:1px solid var(--hair);text-transform:uppercase;
    letter-spacing:.04em}
  th:first-child{text-align:left}
  td{padding:4px 5px;border-bottom:1px solid var(--hair);text-align:right;font-size:12px}
  td:first-child{text-align:left;white-space:nowrap}
  tr.sel td{background:color-mix(in srgb,var(--hair) 55%,transparent)}
  tbody tr{cursor:pointer}
  .chip{display:inline-block;width:9px;height:9px;border-radius:2px;
    margin-right:6px;vertical-align:baseline;outline:1.5px solid var(--surface)}
  .legend{margin-top:16px;padding-top:12px;border-top:1px solid var(--hair)}
  .lrow{display:flex;align-items:center;gap:7px;margin:4px 0;color:var(--ink2);font-size:12px}
  .swatch{width:20px;height:10px;border-radius:2px;flex:none}
  .dash{width:20px;height:0;border-top:2px dashed var(--muted);flex:none}
  .note{color:var(--muted);font-size:11.5px;margin-top:9px;line-height:1.5}
  .empty{color:var(--muted);font-style:italic;padding:10px 2px}
  .radar-img{image-rendering:pixelated}
  .leaflet-popup-content{font:12px/1.5 ui-sans-serif,system-ui,sans-serif;margin:9px 11px}
  .leaflet-popup-content b{font-size:12.5px}
  .leaflet-popup-content table{margin-top:5px}
  .leaflet-popup-content td{border:0;padding:1px 0}
  .leaflet-popup-content td:first-child{color:#52514e;padding-right:12px}
</style>
</head>
<body>
<header>
  <h1>Convective systems — gl-tracker</h1>
  <div class="sub" id="sub"></div>
</header>

<div class="controls">
  <div class="grp">
    <span class="lbl">Frame</span>
    <button id="prev" title="Previous volume">&larr;</button>
    <span id="frameLabel"></span>
    <button id="next" title="Next volume">&rarr;</button>
    <input type="range" id="slider" min="0" value="0" step="1"/>
  </div>
  <div class="grp">
    <button id="radarBtn" aria-pressed="true">Radar</button>
    <input type="range" id="opacity" min="0" max="100" value="70" title="Reflectivity opacity"/>
  </div>
  <div class="grp">
    <button id="allBtn" aria-pressed="false">All frames</button>
    <button id="fcBtn" aria-pressed="true">Forecasts</button>
    <button id="themeBtn" aria-pressed="false">Dark</button>
    <button id="fitBtn">Fit</button>
  </div>
</div>

<main>
  <div id="map"></div>
  <aside>
    <h2>Systems in frame</h2>
    <div id="tableWrap"></div>
    <div class="legend" id="legend"></div>
  </aside>
</main>

<script>
const DATA = /*__DATA__*/;
const S = DATA.systems, F = DATA.forecasts, FRAMES = DATA.frames;
let frameIdx = 0, showAll = false, showFc = true, dark = false, selected = null;
let showRadar = true, radarOpacity = 0.7;

const map = L.map('map', {preferCanvas:true});
let tiles = null;
// Esri Gray Canvas: no API key, and unlike OSM's volunteer servers it does not
// refuse file:// origins. Deliberately not the argenmap tiles webmet25 uses, so
// this prototype never leans on OHMC's own server. Note Esri orders the path
// {z}/{y}/{x}, not {z}/{x}/{y}. The grey basemaps are built for data overlay,
// which is why the storm hues stay legible on both.
const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/';
const TILE = {
  light: ESRI + 'World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}',
  dark:  ESRI + 'World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}'
};
function setTiles(){
  if (tiles) map.removeLayer(tiles);
  tiles = L.tileLayer(TILE[dark ? 'dark' : 'light'], {
    attribution: 'Tiles &copy; Esri', maxZoom: 16
  }).addTo(map);
  tiles.bringToBack();
}

const palette = () => dark ? DATA.paletteDark : DATA.paletteLight;
const otherCol = () => dark ? DATA.otherDark : DATA.otherLight;
function colorOf(name){
  const slot = DATA.trackSlot[name];
  return (slot === undefined || slot < 0) ? otherCol() : palette()[slot];
}
const shortId = n => String(n).slice(0, 6);
const fmt = (v, d=1) => (v === null || v === undefined) ? '—'
  : (typeof v === 'number' ? v.toFixed(d) : String(v));

const radarLayer = L.layerGroup().addTo(map);   // under the polygons
const sysLayer = L.layerGroup().addTo(map);
const fcLayer  = L.layerGroup().addTo(map);
let siteMarker = null;

function popupHtml(s){
  const rows = [
    ['track', shortId(s.name) + (DATA.multiFrame.includes(s.name) ? ' (tracked)' : '')],
    ['time', s.date_time],
    ['max dBZ', fmt(s.max)],
    ['mean dBZ', fmt(s.mean)],
    ['echo top', s.echo_top_max_m == null ? '—' : fmt(s.echo_top_max_m,0)+' m'],
    ['core area', s.core_area_km2 == null ? '—' : fmt(s.core_area_km2,0)+' km²'],
    ['core RHOHV', fmt(s.core_rhohv_mean, 2)],
    ['dist to radar', s.dist_centroid_km == null ? '—' : fmt(s.dist_centroid_km)+' km'],
    ['convective', s.is_convective ? 'yes' : 'no'],
    ['event', s.event || '—'],
  ];
  return '<b>System ' + shortId(s.name) + '</b><table>' +
    rows.map(([k,v]) => '<tr><td>'+k+'</td><td><b>'+v+'</b></td></tr>').join('') +
    '</table>';
}

function visibleSystems(){
  return showAll ? S : S.filter(s => s.date_time === FRAMES[frameIdx]);
}

function draw(){
  sysLayer.clearLayers(); fcLayer.clearLayers(); radarLayer.clearLayers();
  const vis = visibleSystems();

  // COLMAX for the active frame. In "all frames" mode only the last volume is
  // drawn: stacking every frame's reflectivity would obscure the polygons that
  // are the point of the view.
  if (showRadar){
    const key = showAll ? FRAMES[FRAMES.length-1] : FRAMES[frameIdx];
    const ov = DATA.overlays[key];
    if (ov) L.imageOverlay(ov.url, ov.bounds, {opacity: radarOpacity,
            interactive:false, className:'radar-img'}).addTo(radarLayer);
  }

  vis.forEach(s => {
    const c = colorOf(s.name);
    const isSel = selected === s.name;
    // 2px surface ring keeps overlapping polygons separable, and gives the
    // lower-contrast hues a visible edge against the basemap.
    L.geoJSON(s.geometry, {style:{
        color:'#fcfcfb', weight:4, opacity:dark?0.5:0.9, fill:false}}).addTo(sysLayer);
    L.geoJSON(s.geometry, {style:{
        color:c, weight:isSel?3.5:2, opacity:1,
        fillColor:c, fillOpacity:isSel?0.42:(showAll?0.16:0.26)}})
      .bindPopup(popupHtml(s))
      .on('click', () => { selected = s.name; render(); })
      .addTo(sysLayer);
  });

  if (showFc){
    const names = new Set(vis.map(s => s.name));
    const times = new Set(vis.map(s => s.date_time));
    F.filter(f => names.has(f.name) || (showAll && times.size > 0))
     .filter(f => names.has(f.name))
     .forEach(f => {
       L.geoJSON(f.geometry, {style:{
         color: colorOf(f.name), weight:1.5, opacity:0.85,
         dashArray:'5,4', fill:false}})
        .bindPopup('<b>Forecast +'+f.horizon+' min</b><br>track '+shortId(f.name)+
                   '<br>valid '+f.date_time)
        .addTo(fcLayer);
     });
  }
}

function drawTable(){
  const vis = visibleSystems();
  const wrap = document.getElementById('tableWrap');
  if (!vis.length){ wrap.innerHTML = '<div class="empty">No systems in this frame.</div>'; return; }
  const head = '<tr><th>Track</th><th>max dBZ</th><th>top (m)</th><th>core km²</th><th>dist km</th></tr>';
  const body = vis.map(s =>
    '<tr data-n="'+s.name+'" class="'+(selected===s.name?'sel':'')+'">'+
    '<td><span class="chip" style="background:'+colorOf(s.name)+'"></span>'+shortId(s.name)+'</td>'+
    '<td>'+fmt(s.max)+'</td>'+
    '<td>'+(s.echo_top_max_m==null?'—':fmt(s.echo_top_max_m,0))+'</td>'+
    '<td>'+(s.core_area_km2==null?'—':fmt(s.core_area_km2,0))+'</td>'+
    '<td>'+(s.dist_centroid_km==null?'—':fmt(s.dist_centroid_km))+'</td></tr>').join('');
  wrap.innerHTML = '<table><thead>'+head+'</thead><tbody>'+body+'</tbody></table>';
  wrap.querySelectorAll('tbody tr').forEach(tr => tr.onclick = () => {
    selected = (selected === tr.dataset.n) ? null : tr.dataset.n; render();
  });
}

function drawLegend(){
  const vis = visibleSystems();
  const names = [...new Set(vis.map(s => s.name))];
  let h = '<h2>Legend</h2>';
  h += names.slice(0,8).map(n =>
    '<div class="lrow"><span class="swatch" style="background:'+colorOf(n)+'"></span>'+
    'track '+shortId(n)+(DATA.multiFrame.includes(n)?' · tracked':'')+'</div>').join('');
  if (names.length > 8)
    h += '<div class="lrow"><span class="swatch" style="background:'+otherCol()+'"></span>other ('+(names.length-8)+')</div>';
  h += '<div class="lrow"><span class="dash"></span>forecast (+9…63 min)</div>';
  if (Object.keys(DATA.overlays).length)
    h += '<div class="lrow"><span class="swatch" style="background:linear-gradient('+
         'to right,#5ebaff,#00d400,#f7f700,#ff0000,#d400d4)"></span>COLMAX −20…70 dBZ</div>';
  h += '<div class="note">One colour per track, held across frames: a polygon that '+
       'keeps its colour from one frame to the next is the same system, associated '+
       'by overlap. Dashed outlines are projected positions.</div>';
  document.getElementById('legend').innerHTML = h;
}

function render(){
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  const nHere = showAll ? S.length
                       : S.filter(s => s.date_time === FRAMES[frameIdx]).length;
  document.getElementById('frameLabel').textContent =
    showAll ? FRAMES.length + ' frames'
            : (FRAMES[frameIdx] || '—').replace('+00:00',' UTC')
              + (nHere ? '' : '  · no systems');
  document.getElementById('slider').value = frameIdx;
  document.getElementById('prev').disabled = showAll || frameIdx === 0;
  document.getElementById('next').disabled = showAll || frameIdx >= FRAMES.length-1;
  document.getElementById('allBtn').setAttribute('aria-pressed', showAll);
  document.getElementById('fcBtn').setAttribute('aria-pressed', showFc);
  document.getElementById('radarBtn').setAttribute('aria-pressed', showRadar);
  document.getElementById('radarBtn').disabled = !Object.keys(DATA.overlays).length;
  document.getElementById('themeBtn').setAttribute('aria-pressed', dark);
  document.getElementById('themeBtn').textContent = dark ? 'Light' : 'Dark';
  draw(); drawTable(); drawLegend();
}

function fit(){
  const all = L.geoJSON(S.map(s => s.geometry));
  const b = all.getBounds();
  if (b.isValid()) map.fitBounds(b.pad(0.25)); else map.setView([-31.4,-64.2], 8);
}

// init
setTiles();
if (DATA.site){
  siteMarker = L.circleMarker([DATA.site.lat, DATA.site.lon], {
    radius:5, color:'#0b0b0b', weight:2, fillColor:'#fcfcfb', fillOpacity:1
  }).bindTooltip(DATA.site.name+' antenna', {permanent:false}).addTo(map);
  L.circle([DATA.site.lat, DATA.site.lon], {radius:150000, color:'#898781',
    weight:1, dashArray:'3,5', fill:false, opacity:0.75})
   .bindTooltip('r_max 150 km').addTo(map);
}
const sl = document.getElementById('slider');
sl.max = Math.max(0, FRAMES.length-1);
sl.oninput = e => { frameIdx = +e.target.value; showAll = false; render(); };
document.getElementById('prev').onclick = () => { if (frameIdx>0){frameIdx--; render();} };
document.getElementById('next').onclick = () => { if (frameIdx<FRAMES.length-1){frameIdx++; render();} };
document.getElementById('allBtn').onclick = () => { showAll = !showAll; render(); };
document.getElementById('radarBtn').onclick = () => { showRadar = !showRadar; render(); };
document.getElementById('opacity').oninput = e => {
  radarOpacity = (+e.target.value)/100;
  radarLayer.eachLayer(l => l.setOpacity(radarOpacity));
};
document.getElementById('fcBtn').onclick = () => { showFc = !showFc; render(); };
document.getElementById('themeBtn').onclick = () => { dark = !dark; setTiles(); render(); };
document.getElementById('fitBtn').onclick = fit;
document.addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft' && frameIdx>0){ frameIdx--; showAll=false; render(); }
  if (e.key === 'ArrowRight' && frameIdx<FRAMES.length-1){ frameIdx++; showAll=false; render(); }
});

const nFc = F.length, nTracked = DATA.multiFrame.length;
document.getElementById('sub').textContent =
  `${S.length} system${S.length===1?'':'s'} · ${FRAMES.length} volumes ` +
  `(${new Set(S.map(s=>s.date_time)).size} with systems) · ` +
  `${nTracked} multi-frame track${nTracked===1?'':'s'} · ${nFc} forecast polygon${nFc===1?'':'s'}` +
  (DATA.site ? ` · ${DATA.site.name} at ${DATA.site.lat.toFixed(3)}, ${DATA.site.lon.toFixed(3)}` : '');

fit(); render();
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path,
                    default=Path("/workspace/tracking_out/systems-db.sqlite"))
    ap.add_argument("--grids", type=Path, default=Path("/workspace/grids"))
    ap.add_argument("--no-overlay", action="store_true",
                    help="skip the COLMAX reflectivity overlays")
    ap.add_argument("--min-dbz", type=float, default=5.0,
                    help="hide reflectivity below this value (default: 5)")
    ap.add_argument("--out", type=Path,
                    default=Path("/workspace/tracking_out/tracking_map.html"))
    args = ap.parse_args()

    if not args.db.is_file():
        sys.exit(f"ERROR: no database at {args.db} — run the tracker first.")

    con = _connect(args.db)
    cur = con.cursor()
    systems = load_systems(cur)
    forecasts = load_forecasts(cur)
    con.close()

    if not systems:
        sys.exit("ERROR: the systems table is empty — nothing to map.")

    site = radar_site(args.grids)
    overlays = {} if args.no_overlay else build_overlays(args.grids, args.min_dbz)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_html(systems, forecasts, site, overlays), encoding="utf-8")

    frames = sorted({s["date_time"] for s in systems})
    tracks = {s["name"] for s in systems}
    seen: dict[str, int] = {}
    for s in systems:
        seen[s["name"]] = seen.get(s["name"], 0) + 1
    multi = sum(1 for v in seen.values() if v > 1)

    print(f"systems   : {len(systems)}")
    print(f"frames    : {len(frames)} with systems  ({frames[0]} -> {frames[-1]})")
    print(f"tracks    : {len(tracks)}  ({multi} seen in more than one frame)")
    print(f"forecasts : {len(forecasts)} polygon(s)")
    if overlays:
        print(f"overlays  : {len(overlays)} COLMAX image(s), "
              f"{sum(o['kb'] for o in overlays.values())} KB total")
    print(f"written   : {args.out}  ({args.out.stat().st_size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    _rc = main()
    # mod_spatialite and matplotlib's native libraries abort during interpreter
    # teardown ("double free or corruption") on this image, long after the HTML
    # has been written and closed. os._exit skips that teardown; nothing here
    # relies on atexit handlers.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_rc)
