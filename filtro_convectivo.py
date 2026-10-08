# -*- coding: utf-8 -*-
"""
Created on Tue Aug 11 10:48:45 2026

@author: Denis
Filtro de nucleos convectivos para TATHU sobre la grilla 3D de RMA1 (SINARAME).

Implementa el pipeline del diagrama "Filtro convectivo v0.1" (OHMC):

    A. QC polarimetrico pixel a pixel (RHOHV con proteccion de nucleos + despeckle)
    B. Campo de deteccion: CAPPI + mascara de capa de fusion (banda brillante)
    C. (fuera de este modulo) deteccion TATHU con ThresholdDetector sobre el CAPPI
    D. Clasificador convectivo por objeto: C1 nucleo, C2 desarrollo vertical,
       C3 consistencia polarimetrica, dentro de R_MAX
    E. (fuera de este modulo) histeresis de publicacion sobre eventos TATHU

Convenciones de datos
---------------------
* Los campos 3D llegan como arrays numpy con forma (nz, ny, nx), float32,
  NaN = sin dato (igual que los GeoTIFF de radarlib).
* z_coords: alturas de cada nivel en metros sobre el radar, forma (nz,).
  Con la grilla de 0.6 km: z_coords = np.arange(nz) * 600.0 + z0.
* x_coords, y_coords: coordenadas de la grilla en km respecto del radar,
  formas (nx,) y (ny,).
* h0_m: altura de la isoterma de 0 grados en metros (sondeo SACO o modelo).
  Si es None, el clasificador corre en modo degradado (sin C2 ni mascara
  de capa de fusion) y lo deja registrado en los diagnosticos.

Dependencias: numpy, scipy. El adaptador a TATHU (al final) requiere ademas
shapely y rasterio, ya presentes en el stack de webmet25.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy import ndimage


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

@dataclass
class ConvectiveFilterConfig:
    """Parametros del filtro. Valores iniciales de la tabla del diagrama v0.1."""

    # A - QC polarimetrico
    rhohv_qc_min: float = 0.85     # umbral RHOHV para ecos no meteorologicos
    dbzh_protect: float = 35.0     # sobre este DBZH no se filtra por RHOHV (granizo)
    despeckle_min_px: int = 4      # grupos conexos menores se anulan

    # B - campo de deteccion
    cappi_height_m: float = 2500.0     # nivel del CAPPI de deteccion
    ml_rhohv_lo: float = 0.90          # banda RHOHV de capa de fusion
    ml_rhohv_hi: float = 0.97
    ml_halfwidth_m: float = 1000.0     # H0 +/- este semiancho

    # C - deteccion (los usa el ThresholdDetector de TATHU, se exponen aca
    #     para que toda la parametrizacion viva en un solo lugar)
    det_threshold_dbz: float = 30.0
    det_min_area_km2: float = 10.0

    # D - clasificador por objeto
    core_threshold_dbz: float = 40.0
    core_min_area_km2: float = 8.0
    echotop_dbz: float = 35.0
    echotop_min_above_h0_m: float = 1500.0
    core_rhohv_mean_min: float = 0.80
    r_max_km: float = 150.0

    # geometria de la grilla
    pixel_area_km2: float = 1.0    # 1 km x 1 km


# ---------------------------------------------------------------------------
# A - QC polarimetrico
# ---------------------------------------------------------------------------

def aplicar_qc(dbzh: np.ndarray,
               rhohv: np.ndarray,
               cfg: ConvectiveFilterConfig) -> np.ndarray:
    """QC pixel a pixel sobre el volumen 3D. Devuelve DBZH depurado (copia).

    Regla central: se anula el pixel donde RHOHV < rhohv_qc_min **y ademas**
    DBZH < dbzh_protect. La segunda condicion protege nucleos con granizo o
    fase mixta, donde RHOHV cae por razones meteorologicas. Aplicar el umbral
    de RHOHV sin esta proteccion borra exactamente lo que se quiere seguir.
    """
    dbzh_qc = dbzh.astype(np.float32, copy=True)

    no_meteo = (rhohv < cfg.rhohv_qc_min) & (dbzh < cfg.dbzh_protect)
    dbzh_qc[no_meteo] = np.nan

    # Despeckle nivel por nivel: componentes conexas chicas de eco valido.
    for k in range(dbzh_qc.shape[0]):
        dbzh_qc[k] = _despeckle_2d(dbzh_qc[k], cfg.despeckle_min_px)

    return dbzh_qc


def _despeckle_2d(campo: np.ndarray, min_px: int) -> np.ndarray:
    """Anula componentes conexas (conectividad 8) con menos de min_px pixeles."""
    valido = np.isfinite(campo)
    if not valido.any():
        return campo
    estructura = np.ones((3, 3), dtype=bool)
    etiquetas, n = ndimage.label(valido, structure=estructura)
    if n == 0:
        return campo
    tamanos = ndimage.sum_labels(valido, etiquetas, index=np.arange(1, n + 1))
    chicas = np.flatnonzero(tamanos < min_px) + 1
    if chicas.size:
        campo[np.isin(etiquetas, chicas)] = np.nan
    return campo


# ---------------------------------------------------------------------------
# B - campo de deteccion
# ---------------------------------------------------------------------------

def extraer_cappi(campo_3d: np.ndarray,
                  z_coords: np.ndarray,
                  altura_m: float) -> np.ndarray:
    """Extrae el nivel de la grilla mas cercano a la altura pedida."""
    k = int(np.argmin(np.abs(np.asarray(z_coords) - altura_m)))
    return campo_3d[k].copy()


def mascara_capa_fusion(rhohv: np.ndarray,
                        z_coords: np.ndarray,
                        h0_m: Optional[float],
                        cfg: ConvectiveFilterConfig) -> np.ndarray:
    """Mascara 2D (True = pixel afectado por capa de fusion).

    Firma usada: RHOHV deprimido dentro de la banda [ml_rhohv_lo, ml_rhohv_hi]
    en los niveles comprendidos en H0 +/- ml_halfwidth_m (Ryzhkov y Zrnic, 2019).
    Si h0_m es None devuelve una mascara vacia (modo degradado).
    """
    ny, nx = rhohv.shape[1], rhohv.shape[2]
    if h0_m is None:
        return np.zeros((ny, nx), dtype=bool)

    z = np.asarray(z_coords)
    niveles = np.flatnonzero(np.abs(z - h0_m) <= cfg.ml_halfwidth_m)
    if niveles.size == 0:
        return np.zeros((ny, nx), dtype=bool)

    banda = rhohv[niveles]
    firma = (banda >= cfg.ml_rhohv_lo) & (banda <= cfg.ml_rhohv_hi)
    return firma.any(axis=0)


def campo_deteccion(dbzh_qc: np.ndarray,
                    rhohv: np.ndarray,
                    z_coords: np.ndarray,
                    h0_m: Optional[float],
                    cfg: ConvectiveFilterConfig) -> np.ndarray:
    """CAPPI de deteccion con la capa de fusion enmascarada.

    Este es el campo que se le pasa al ThresholdDetector de TATHU en lugar
    del COLMAX: la banda brillante puede proyectar 35-45 dBZ en el maximo de
    columna siendo precipitacion estratiforme.
    """
    cappi = extraer_cappi(dbzh_qc, z_coords, cfg.cappi_height_m)
    ml = mascara_capa_fusion(rhohv, z_coords, h0_m, cfg)
    cappi[ml] = np.nan
    return cappi


# ---------------------------------------------------------------------------
# D - clasificador por objeto
# ---------------------------------------------------------------------------

def tope_de_eco(dbzh_qc: np.ndarray,
                z_coords: np.ndarray,
                umbral_dbz: float) -> np.ndarray:
    """Altura (m) del nivel mas alto con DBZH >= umbral, por columna.

    Devuelve NaN donde ninguna altura supera el umbral. Con la grilla de
    0.6 km el error de discretizacion vertical es +/- 300 m.
    """
    supera = dbzh_qc >= umbral_dbz                      # (nz, ny, nx)
    z = np.asarray(z_coords, dtype=np.float32)
    # indice del nivel mas alto que supera el umbral
    idx = np.where(supera.any(axis=0),
                   supera.shape[0] - 1 - np.argmax(supera[::-1], axis=0),
                   -1)
    tope = np.full(idx.shape, np.nan, dtype=np.float32)
    con_eco = idx >= 0
    tope[con_eco] = z[idx[con_eco]]
    return tope


def clasificar_objeto(mask_obj: np.ndarray,
                      dbzh_qc: np.ndarray,
                      rhohv: np.ndarray,
                      echo_top: np.ndarray,
                      dist_km: np.ndarray,
                      h0_m: Optional[float],
                      cfg: ConvectiveFilterConfig) -> dict:
    """Aplica C1-C3 a un objeto detectado por TATHU.

    Parametros
    ----------
    mask_obj : bool (ny, nx) - pixeles del poligono del objeto.
    dbzh_qc  : float (nz, ny, nx) - volumen depurado por aplicar_qc().
    rhohv    : float (nz, ny, nx) - RHOHV original (sin QC).
    echo_top : float (ny, nx) - salida de tope_de_eco().
    dist_km  : float (ny, nx) - distancia de cada pixel al radar.

    Devuelve un dict listo para volcar en los `fields` del ConvectiveSystem.
    """
    out = {
        "is_convective": False,
        "c1_core": False,
        "c2_vertical": None,       # None = no evaluado (modo degradado)
        "c3_polarimetric": False,
        "in_domain": False,
        "mode": "full" if h0_m is not None else "degradado",
        "core_area_km2": 0.0,
        "core_max_dbz": np.nan,
        "core_rhohv_mean": np.nan,
        "echo_top_max_m": np.nan,
        "dist_centroid_km": np.nan,
    }

    if not mask_obj.any():
        return out

    # --- dominio: distancia media de los pixeles del objeto -----------------
    out["dist_centroid_km"] = float(np.nanmean(dist_km[mask_obj]))
    out["in_domain"] = out["dist_centroid_km"] <= cfg.r_max_km
    if not out["in_domain"]:
        return out

    # columna maxima dentro del objeto (para C1 y C3 se trabaja en 3D
    # restringido al footprint del poligono). Columnas sin ningun dato valido
    # son esperables dentro del poligono; se silencia el warning de numpy.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        col_max = np.nanmax(np.where(mask_obj[None, :, :], dbzh_qc, np.nan),
                            axis=0)

    # --- C1: nucleo >= core_threshold con area minima -----------------------
    core_2d = mask_obj & (col_max >= cfg.core_threshold_dbz)
    core_area = float(core_2d.sum()) * cfg.pixel_area_km2
    out["core_area_km2"] = core_area
    if core_2d.any():
        out["core_max_dbz"] = float(np.nanmax(col_max[core_2d]))
    out["c1_core"] = core_area >= cfg.core_min_area_km2

    # --- C2: desarrollo vertical (solo con H0 disponible) -------------------
    if h0_m is not None:
        tops = echo_top[mask_obj]
        if np.isfinite(tops).any():
            out["echo_top_max_m"] = float(np.nanmax(tops))
            out["c2_vertical"] = (
                out["echo_top_max_m"] >= h0_m + cfg.echotop_min_above_h0_m
            )
        else:
            out["c2_vertical"] = False

    # --- C3: consistencia polarimetrica del nucleo --------------------------
    if core_2d.any():
        # RHOHV en las columnas del nucleo, donde hay eco del nucleo
        nucleo_3d = (dbzh_qc >= cfg.core_threshold_dbz) & core_2d[None, :, :]
        vals = rhohv[nucleo_3d]
        if vals.size:
            out["core_rhohv_mean"] = float(np.nanmean(vals))
            out["c3_polarimetric"] = (
                out["core_rhohv_mean"] >= cfg.core_rhohv_mean_min
            )

    # --- veredicto ----------------------------------------------------------
    c2_ok = out["c2_vertical"] if out["c2_vertical"] is not None else True
    out["is_convective"] = bool(out["c1_core"] and c2_ok
                                and out["c3_polarimetric"])
    return out


# ---------------------------------------------------------------------------
# Funcion de conveniencia: pipeline completo por volumen
# ---------------------------------------------------------------------------

@dataclass
class VolumenFiltrado:
    """Resultado de preparar un volumen: lo que TATHU necesita."""
    campo_deteccion: np.ndarray          # 2D, para el ThresholdDetector
    dbzh_qc: np.ndarray                  # 3D depurado
    echo_top: np.ndarray                 # 2D
    dist_km: np.ndarray                  # 2D
    h0_m: Optional[float]
    cfg: ConvectiveFilterConfig = field(default_factory=ConvectiveFilterConfig)

    def clasificar(self, mask_obj: np.ndarray, rhohv: np.ndarray) -> dict:
        return clasificar_objeto(mask_obj, self.dbzh_qc, rhohv,
                                 self.echo_top, self.dist_km,
                                 self.h0_m, self.cfg)


def preparar_volumen(dbzh: np.ndarray,
                     rhohv: np.ndarray,
                     x_coords: np.ndarray,
                     y_coords: np.ndarray,
                     z_coords: np.ndarray,
                     h0_m: Optional[float],
                     cfg: Optional[ConvectiveFilterConfig] = None
                     ) -> VolumenFiltrado:
    """Etapas A y B completas sobre un volumen. Uso tipico en el daemon:

        vol = preparar_volumen(dbzh, rhohv, x, y, z, h0)
        systems = detector.detect(vol.campo_deteccion)      # TATHU
        for s in systems:
            s.fields.update(vol.clasificar(rasterizar(s.geom), rhohv))
        systems = [s for s in systems if s.fields["is_convective"]]
        tracker.track(previous, systems)                    # TATHU
    """
    cfg = cfg or ConvectiveFilterConfig()

    dbzh_qc = aplicar_qc(dbzh, rhohv, cfg)
    det = campo_deteccion(dbzh_qc, rhohv, z_coords, h0_m, cfg)
    etop = tope_de_eco(dbzh_qc, z_coords, cfg.echotop_dbz)

    xx, yy = np.meshgrid(np.asarray(x_coords), np.asarray(y_coords))
    dist_km = np.sqrt(xx ** 2 + yy ** 2).astype(np.float32)

    return VolumenFiltrado(campo_deteccion=det, dbzh_qc=dbzh_qc,
                           echo_top=etop, dist_km=dist_km,
                           h0_m=h0_m, cfg=cfg)


# ---------------------------------------------------------------------------
# Adaptador TATHU: Descriptor que aplica el clasificador a cada sistema
# ---------------------------------------------------------------------------

class ConvectiveClassifierDescriptor:
    """Descriptor al estilo TATHU: escribe el veredicto en system.fields.

    Sigue el contrato describe(data, systems) de tathu.tracking.descriptors.
    Requiere shapely y rasterio para rasterizar el poligono de cada sistema
    sobre la grilla (ambos ya estan en el stack de webmet25).

    Uso:
        descriptor = ConvectiveClassifierDescriptor(vol, rhohv, transform)
        descriptor.describe(systems)
        systems = [s for s in systems if s.fields.get("is_convective")]
    """

    def __init__(self, vol: VolumenFiltrado, rhohv: np.ndarray, transform):
        """transform: affine.Affine de la grilla (el mismo del GeoTIFF)."""
        self.vol = vol
        self.rhohv = rhohv
        self.transform = transform
        self.shape = vol.campo_deteccion.shape

    def describe(self, systems) -> None:
        from shapely import wkt as shapely_wkt
        from rasterio import features as rio_features

        for system in systems:
            geom = shapely_wkt.loads(system.geom.ExportToWkt())
            mask = rio_features.rasterize(
                [(geom, 1)], out_shape=self.shape,
                transform=self.transform, fill=0, dtype="uint8",
            ).astype(bool)
            system.fields.update(self.vol.clasificar(mask, self.rhohv))


def filtrar_convectivos(systems) -> list:
    """Etapa E parcial: descarta los objetos no convectivos antes del Tracker.

    La histeresis de publicacion (CANDIDATO/ACTIVO/DISIPACION) no vive aca:
    se resuelve despues del tracking, leyendo los eventos de ciclo de vida
    de TATHU, porque necesita la asociacion temporal entre volumenes.
    """
    return [s for s in systems if s.fields.get("is_convective", False)]