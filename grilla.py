# -*- coding: utf-8 -*-
"""Entrada del operativo: la grilla de radar ya interpolada.

El grillado lo hace otro paso. Este modulo descubre las grillas nuevas, las lee
y corre sobre ellas las etapas A y B del filtro (QC polarimetrico, campo de
deteccion con la capa de fusion enmascarada, tope de eco). Es el unico modulo
que conoce el formato del archivo de entrada.

Contrato de entrada
-------------------
Un netCDF por volumen, con:

  * dos variables 3D (nz, ny, nx) float32, NaN = sin dato: reflectividad y
    RHOHV. Los nombres se configuran en `Config.var_dbzh` / `var_rhohv`.
  * coordenadas 1D: `x_km`, `y_km` (km desde el radar) y `z_m` (altura). Si
    vienen en metros con nombres `x`/`y`, se convierten solas.
  * fila 0 = norte (convenio GDAL). Si la grilla viene con fila 0 = sur
    (convenio Py-ART), poner `Config.invertir_y = True`.

Y de forma opcional, como atributos globales:

  * `referencia_z`  -> "radar" o "msnm". Si no esta, se usa `Config.referencia_z`.
  * `timestamp`     -> ISO 8601. Si no esta, se saca del nombre del archivo.
  * `radar_lat`, `radar_lon`, `radar_alt_m` -> si no estan, se usan los de Config.

Lo unico que no se puede adivinar es `referencia_z`. Si el archivo no lo trae y
Config no coincide con la realidad, C2 se corre con 484 m de error y no hay
sintoma visible: el filtro simplemente descarta o retiene de mas. Por eso el
lector avisa cuando tiene que caer al valor de Config.
"""

from __future__ import annotations

import re
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from netCDF4 import Dataset
from osgeo import gdal, osr

import gate                       # hace el bootstrap de sys.path
import filtro_convectivo as fc    # noqa: E402
from tathu.constants import METERS_PER_DEGREE  # noqa: E402

# En GDAL 4.0 las excepciones pasan a estar activadas por defecto. Declararlo
# ahora evita el FutureWarning y fija el comportamiento: un error de GDAL tiene
# que levantar excepcion, no devolver None en silencio.
gdal.UseExceptions()
osr.UseExceptions()

_TIMESTAMP_RE = re.compile(r"(\d{8}T\d{6})Z?")


# --------------------------------------------------------------------------
# Descubrimiento
# --------------------------------------------------------------------------

def timestamp_de_nombre(ruta: Path) -> Optional[datetime]:
    """Fecha/hora UTC desde el nombre del archivo.

    Se prefiere al atributo interno porque esta disponible sin abrir nada, que
    es lo que permite ordenar y filtrar candidatos sin leer cientos de MB.
    """
    m = _TIMESTAMP_RE.search(ruta.name)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)


def esta_cerrado(ruta: Path, edad_minima: float) -> bool:
    """True si hace `edad_minima` segundos que nadie escribe el archivo.

    Una grilla a medio escribir se lee sin error y da un volumen truncado. Con
    C2 como filtro unico eso significa un tope de eco incompleto y, muy
    probablemente, descartar el volumen entero sin que nadie se entere.
    """
    try:
        return (time.time() - ruta.stat().st_mtime) >= edad_minima
    except OSError:
        return False


def listar(cfg) -> list[tuple[datetime, Path]]:
    """(timestamp, ruta) de todas las grillas utilizables, de la mas vieja a la mas nueva."""
    if not cfg.grilla_raiz.is_dir():
        raise FileNotFoundError(f"No existe el directorio de grillas: {cfg.grilla_raiz}")
    salida = []
    for ruta in cfg.grilla_raiz.glob(cfg.grilla_glob):
        ts = timestamp_de_nombre(ruta)
        if ts is not None:
            salida.append((ts, ruta))
    return sorted(salida)


def descubrir_pendientes(cfg, desde: Optional[datetime]) -> list[tuple[datetime, Path]]:
    """Grillas cerradas posteriores a `desde`, en orden cronologico.

    Devuelve una lista y no solo la ultima a proposito: si el proceso estuvo
    caido un rato, recorrerlas en orden reconstruye las trayectorias en vez de
    saltar al presente y perder la continuidad.
    """
    pendientes = []
    for ts, ruta in listar(cfg):
        if desde is not None and ts <= desde:
            continue
        if not esta_cerrado(ruta, cfg.grilla_edad_minima):
            continue
        pendientes.append((ts, ruta))
    return pendientes


# --------------------------------------------------------------------------
# Lectura
# --------------------------------------------------------------------------

@dataclass
class VolumenGrillado:
    """Una grilla leida: el raster 2D que consume TATHU + el 3D que consume el filtro."""
    timestamp: datetime
    raster: object              # GDAL, EPSG:4326 — entrada del detector
    transform: object           # affine.Affine equivalente al geotransform
    vol: fc.VolumenFiltrado     # etapas A y B del filtro
    rhohv: np.ndarray           # 3D (nz, ny, nx), sin QC
    lat0: float
    lon0: float
    alt0: float
    referencia_z: str


def _leer_var(nc, nombres, que: str):
    """Primera variable que exista de una lista de nombres candidatos."""
    for n in nombres:
        if n in nc.variables:
            return n
    raise KeyError(f"no se encontro {que}; se probo {list(nombres)}. "
                   f"Variables presentes: {list(nc.variables)}")


def _coordenadas(nc, cfg):
    """(x_km, y_km, z_m) tolerando nombres y unidades distintas."""
    # x, y: se aceptan en km o en metros. El nombre con sufijo _km es el del
    # contrato; `x`/`y` a secas se asume Py-ART, que escribe metros.
    if "x_km" in nc.variables:
        x_km = np.asarray(nc.variables["x_km"][:], dtype="float64")
    else:
        n = _leer_var(nc, ("x", "point_x", "lon_km"), "la coordenada x")
        x_km = np.asarray(nc.variables[n][:], dtype="float64")
        if np.nanmax(np.abs(x_km)) > 5000.0:     # claramente metros
            x_km = x_km / 1000.0

    if "y_km" in nc.variables:
        y_km = np.asarray(nc.variables["y_km"][:], dtype="float64")
    else:
        n = _leer_var(nc, ("y", "point_y", "lat_km"), "la coordenada y")
        y_km = np.asarray(nc.variables[n][:], dtype="float64")
        if np.nanmax(np.abs(y_km)) > 5000.0:
            y_km = y_km / 1000.0

    n = _leer_var(nc, ("z_m", "z", "height", "altitude"), "la coordenada z")
    z_m = np.asarray(nc.variables[n][:], dtype="float64")

    return x_km, y_km, z_m


def _build_raster(data2d, lat0, lon0, x_km, y_km, fill_value):
    """Envuelve un campo 2D (fila 0 = norte) en un raster GDAL georreferenciado.

    Los NaN se reemplazan por `fill_value` ANTES de escribir la banda: el
    detector de TATHU compara con `data[data <= umbral] = 0`, y toda comparacion
    contra NaN da False, asi que un NaN sobreviviria al umbral y `ndimage.label`
    lo etiquetaria como parte de un objeto.
    """
    data = np.where(np.isfinite(data2d), data2d, fill_value).astype(np.float32)
    ny, nx = data.shape

    x_m, y_m = x_km * 1000.0, y_km * 1000.0
    m_per_deg_lon = METERS_PER_DEGREE * np.cos(np.radians(lat0))
    dx_deg = float(x_m[1] - x_m[0]) / m_per_deg_lon
    dy_deg = float(y_m[1] - y_m[0]) / METERS_PER_DEGREE

    # Medio pixel de correccion: geotransform = esquina, las coordenadas = centro.
    gt = (lon0 + x_m[0] / m_per_deg_lon - dx_deg / 2.0, dx_deg, 0.0,
          lat0 + y_m[-1] / METERS_PER_DEGREE + dy_deg / 2.0, 0.0, -dy_deg)

    raster = gdal.GetDriverByName("MEM").Create("", nx, ny, 1, gdal.GDT_Float32)
    raster.SetGeoTransform(gt)

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)   # (lon, lat)
    raster.SetProjection(srs.ExportToWkt())

    banda = raster.GetRasterBand(1)
    banda.WriteArray(data)
    banda.SetNoDataValue(fill_value)
    raster.FlushCache()
    return raster


def leer(ruta: Path, timestamp: datetime, cfg, h0_msnm: float) -> VolumenGrillado:
    """Lee la grilla y corre las etapas A y B del filtro."""
    with Dataset(ruta, "r") as nc:
        n_dbz = _leer_var(nc, (cfg.var_dbzh, "DBZH", "reflectivity", "DBZ"),
                          "la reflectividad")
        n_rho = _leer_var(nc, (cfg.var_rhohv, "RHOHV", "cross_correlation_ratio"),
                          "RHOHV")

        dbzh = np.asarray(nc.variables[n_dbz][:], dtype="float32")
        rhohv = np.asarray(nc.variables[n_rho][:], dtype="float32")

        # Py-ART escribe (time, z, y, x). Se toma el unico paso de tiempo.
        if dbzh.ndim == 4:
            dbzh, rhohv = dbzh[0], rhohv[0]
        if dbzh.ndim != 3:
            raise ValueError(f"{ruta.name}: {n_dbz} tiene forma {dbzh.shape}, se esperaba (nz, ny, nx)")

        # El fill_value del netCDF puede llegar como valor centinela en vez de
        # NaN. El filtro exige el convenio NaN = sin dato: sin el, el despeckle
        # del QC no distingue aire limpio de eco.
        for arr, var in ((dbzh, nc.variables[n_dbz]), (rhohv, nc.variables[n_rho])):
            relleno = getattr(var, "_FillValue", None)
            if relleno is not None and np.isfinite(relleno):
                arr[arr == relleno] = np.nan

        x_km, y_km, z_m = _coordenadas(nc, cfg)

        if cfg.invertir_y:
            dbzh = dbzh[:, ::-1, :]
            rhohv = rhohv[:, ::-1, :]
            y_km = y_km[::-1]

        referencia_z = getattr(nc, "referencia_z", None)
        if referencia_z is None:
            referencia_z = cfg.referencia_z
            warnings.warn(
                f"{ruta.name}: sin atributo 'referencia_z'; se asume "
                f"'{referencia_z}' de Config. Si la grilla mide z desde otra "
                f"referencia, C2 corre con el error de esa diferencia.",
                RuntimeWarning, stacklevel=2)

        lat0 = float(getattr(nc, "radar_lat", cfg.radar_lat))
        lon0 = float(getattr(nc, "radar_lon", cfg.radar_lon))
        alt0 = float(getattr(nc, "radar_alt_m", cfg.radar_alt_m))

    if not np.isfinite(rhohv).any():
        # Sin RHOHV utilizable no hay QC polarimetrico ni mascara de capa de
        # fusion, y el tope de eco saldria de reflectividad cruda. Antes que
        # filtrar con un tope contaminado, se corta y se espera la siguiente.
        raise ValueError(f"{ruta.name}: {n_rho} viene entero vacio. "
                         f"Sin RHOHV el QC no corre y el tope de eco no es confiable.")

    filtro_cfg = cfg.filtro()
    h0_grilla = gate.h0_para_grilla(h0_msnm, referencia_z, alt0)
    vol = fc.preparar_volumen(dbzh, rhohv, x_km, y_km, z_m, h0_grilla, filtro_cfg)

    raster = _build_raster(vol.campo_deteccion, lat0, lon0, x_km, y_km, cfg.fill_value)

    from affine import Affine
    return VolumenGrillado(
        timestamp=timestamp,
        raster=raster,
        transform=Affine.from_gdal(*raster.GetGeoTransform()),
        vol=vol,
        rhohv=rhohv,
        lat0=lat0,
        lon0=lon0,
        alt0=alt0,
        referencia_z=referencia_z,
    )
