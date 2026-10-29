# -*- coding: utf-8 -*-
"""Procesamiento de un volumen: gate, deteccion, clasificacion, tracking, salida.

El orden importa por costo. Rasterizar el poligono de un sistema y hacer
reducciones numpy sobre su footprint son milisegundos; `StatisticalDescriptor`
recorta el raster por sistema, y tracking + forecast generan 7 poligonos extra
por sistema y otras tantas filas de base. Por eso lo no convectivo se descarta
antes de todo eso — y por eso el gate, que descarta el volumen ENTERO, va antes
incluso de detectar.
"""

from __future__ import annotations

import copy
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
from rasterio import features as rio_features
from shapely import wkt as shapely_wkt

import filtro_convectivo as fc
from tathu.constants import KM_PER_DEGREE, METERS_PER_DEGREE
from tathu.geometry import transform
from tathu.io import spatialite
from tathu.tracking import descriptors, detectors, forecasters, trackers
from tathu.tracking.system import LifeCycleEvent

import estado as estado_mod
import gate
import grilla as grilla_mod
from config import CONV_ATTRS, DB_ATTRS, MOVEMENT_ATTRS, STAT_ATTRS

STRATEGIES = {
    "relative": lambda c: trackers.RelativeOverlapAreaStrategy(c.overlap),
    "absolute": lambda c: trackers.AbsoluteOverlapAreaStrategy(c.overlap),
    "titan": lambda c: trackers.TitanStrategy(c.titan_threshold),
    "intersects": lambda c: trackers.IntersectsStrategy(),
}

WEIGHTS_FULL = np.array([0.2, 0.3, 0.5, 1.0])
MIN_POINTS_FOR_FIT = 2


# --------------------------------------------------------------------------
# Salidas
# --------------------------------------------------------------------------

class Salidas:
    """Conexiones a la base, abiertas una vez y reutilizadas.

    `Outputter.output()` hace commit en cada llamada y la creacion de tablas es
    idempotente, asi que mantenerlas abiertas entre volumenes es seguro y evita
    reabrir SpatiaLite ~160 veces por dia.
    """

    def __init__(self, cfg):
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        self.systems = spatialite.Outputter(str(cfg.db_path), "systems", DB_ATTRS)
        self.forecasts = {
            t: spatialite.Outputter(str(cfg.db_path), f"forecast_{t}min", DB_ATTRS)
            for t in cfg.forecast_intervals
        }


# --------------------------------------------------------------------------
# Velocidad ponderada por historial
# --------------------------------------------------------------------------

def actualizar_historia(historia, systems):
    """Agrega (timestamp, cx, cy, tasa de crecimiento) de cada sistema.

    La tasa es la de TATHU (`compute_scale_factor`): cambio relativo de AREA por
    minuto respecto de la suma de las areas previas, asi una fusion no cuenta
    como crecimiento. None si no hay dt. Las entradas viejas del pickle tienen
    solo 3 elementos; `tasa_crecimiento` las ignora.
    """
    for s in systems:
        c = s.geom.Centroid()
        elapsed = forecasters.compute_elapsed_time(s)
        tasa = forecasters.compute_scale_factor(s, elapsed) if elapsed else None
        historia[s.name].append((s.timestamp, c.GetX(), c.GetY(), tasa))


def tasa_crecimiento(historia, name):
    """Tasa de crecimiento de area (1/min) suavizada sobre el historial.

    Promedio ponderado con los mismos pesos que la velocidad (el ultimo paso
    pesa mas). None si no hay ninguna tasa registrada.
    """
    tasas = [p[3] for p in historia.get(name, ()) if len(p) > 3 and p[3] is not None]
    tasas = tasas[-len(WEIGHTS_FULL):]
    if not tasas:
        return None
    return float(np.average(tasas, weights=WEIGHTS_FULL[-len(tasas):]))


def factor_escala(tasa, t, minimo, maximo):
    """Factor LINEAL (ancho y alto) a aplicar al poligono a t minutos.

    `tasa` es crecimiento de AREA, asi que el area a t minutos es
    (1 + tasa*t) veces la actual y cada lado escala por su raiz. TATHU aplica
    (1 + tasa*t) a cada lado, lo que eleva el area al cuadrado; y con tasas
    negativas el factor cruza cero y shapely espeja el poligono. Por eso la
    raiz, y el recorte a [minimo, maximo].
    """
    area = max(1.0 + tasa * t, 0.0)
    return float(min(max(np.sqrt(area), minimo), maximo))


def ajustar_velocidad(historia, name):
    """(u, v) en grados/seg por minimos cuadrados ponderados.

    None si no hay al menos MIN_POINTS_FOR_FIT puntos para este sistema.
    """
    pts = historia.get(name)
    if pts is None or len(pts) < MIN_POINTS_FOR_FIT:
        return None
    n = len(pts)
    t0 = pts[0][0]
    t = np.array([(p[0] - t0).total_seconds() for p in pts])
    x = np.array([p[1] for p in pts])
    y = np.array([p[2] for p in pts])
    w = WEIGHTS_FULL[-n:]
    return float(np.polyfit(t, x, 1, w=w)[0]), float(np.polyfit(t, y, 1, w=w)[0])


def aplicar_velocidad_historia(historia, matched):
    """Sobrescribe u/v y velocity en los sistemas con historia suficiente.

    OJO: esto solo actualiza columnas DESCRIPTIVAS de la tabla `systems`, para
    inspeccion y QC. `forecasters.Conservative.forecast()` no lee s.u/s.v: usa
    su propio delta de 2 puntos. Para que el historial afecte el pronostico de
    verdad hay que usar `pronosticar_con_historia()`.
    """
    for s in matched:
        fit = ajustar_velocidad(historia, s.name)
        if fit is None:
            continue
        u, v = fit
        rapidez = float(np.hypot(u, v))
        if rapidez == 0:
            continue
        s.u = u / rapidez
        s.v = v / rapidez
        s.velocity = rapidez * METERS_PER_DEGREE


def pronosticar_con_historia(current, historia, intervals, apply_scale=True,
                             escala_min=0.5, escala_max=2.0):
    """Como forecasters.Conservative.forecast(), con velocidad y crecimiento
    ponderados sobre el historial.

    Reemplaza el delta de 2 puntos que TATHU calcula internamente por la
    velocidad ajustada sobre el historial, para los sistemas que tienen
    suficientes puntos. El crecimiento tambien sale del historial
    (`tasa_crecimiento`) y se aplica con `factor_escala`, no como en TATHU (ver
    su docstring). Con un historial vacio se comporta como Conservative salvo
    por esa correccion de la escala.

    La unidad interna de `elapsedtime` no se asume: se autocalibra por sistema
    comparando el dt real en segundos contra compute_elapsed_time().
    """
    forecasts = {t: [] for t in intervals}

    for sys_ in current:
        if sys_.event in (LifeCycleEvent.SPONTANEOUS_GENERATION, LifeCycleEvent.SPLIT):
            continue

        elapsed = forecasters.compute_elapsed_time(sys_)
        if elapsed == 0.0:
            continue

        dt_s = (sys_.timestamp - sys_.relationships[0].timestamp).total_seconds()
        k = dt_s / elapsed if elapsed else None

        fit = ajustar_velocidad(historia, sys_.name)
        if fit is not None and k:
            u, v = fit
            dx, dy = u * k, v * k
        else:
            base = forecasters.compute_last_centroid(sys_)
            dx, dy = forecasters.delta(sys_.geom.Centroid(), base, elapsed)

        tasa = tasa_crecimiento(historia, sys_.name)
        if tasa is None:
            tasa = forecasters.compute_scale_factor(sys_, elapsed)

        for t in intervals:
            prevision = copy.deepcopy(sys_)
            prevision.geom = transform.translate(sys_.geom, dx * t, dy * t)
            if apply_scale:
                f = factor_escala(tasa, t, escala_min, escala_max)
                prevision.geom = transform.scale(prevision.geom, f, f)
            forecasts[t].append(prevision)

    return forecasts


# --------------------------------------------------------------------------
# Deteccion y clasificacion
# --------------------------------------------------------------------------

def km2_a_deg2(area_km2, lat):
    """Area en km2 -> area equivalente en grados2, a la latitud dada."""
    return area_km2 / (KM_PER_DEGREE * KM_PER_DEGREE * np.cos(np.radians(lat)))


def clasificar_sistemas(systems, v, cfg, h0_msnm, h0_fuente):
    """Escribe el veredicto en `system.attrs` de cada objeto.

    Se calculan C1, C2 y C3 completos porque cuestan milisegundos y son el
    unico registro que permite despues re-evaluar umbrales sobre corridas
    viejas sin reprocesar volumenes. Pero con `cfg.solo_c2` el VEREDICTO lo
    decide C2 solo.

    `c2_vertical is True` y no `if attrs["c2_vertical"]` a proposito: si alguna
    vez volviera a existir un camino donde H0 no esta, C2 quedaria en None y la
    verdad de Python lo trataria como False. Preferible que sea explicito.
    """
    forma = v.vol.campo_deteccion.shape
    for s in systems:
        geom = shapely_wkt.loads(s.geom.ExportToWkt())
        mask = rio_features.rasterize([(geom, 1)], out_shape=forma,
                                      transform=v.transform, fill=0,
                                      dtype="uint8").astype(bool)
        attrs = v.vol.clasificar(mask, v.rhohv)
        attrs["h0_msnm"] = h0_msnm
        attrs["h0_fuente"] = h0_fuente
        if cfg.solo_c2:
            attrs["is_convective"] = bool(attrs["in_domain"]
                                          and attrs["c2_vertical"] is True)
        s.attrs.update(attrs)


# --------------------------------------------------------------------------
# Resultado
# --------------------------------------------------------------------------

@dataclass
class Resultado:
    timestamp: datetime
    archivo: str
    gate: gate.ResultadoGate
    detectados: int = 0
    convectivos: int = 0
    rastreados: int = 0
    forecasts: int = 0
    reinicio_tracking: bool = False
    segundos: float = 0.0

    def linea(self) -> str:
        if not self.gate.pasa:
            return (f"{self.timestamp:%Y-%m-%d %H:%M:%S}  DESCARTADO  {self.gate}  "
                    f"[{self.segundos:.1f}s]")
        marca = "  (tracking reiniciado)" if self.reinicio_tracking else ""
        return (f"{self.timestamp:%Y-%m-%d %H:%M:%S}  detectados={self.detectados:3d}  "
                f"convectivos={self.convectivos:3d}  rastreados={self.rastreados:3d}  "
                f"forecasts={self.forecasts:3d}  H0={self.gate.h0_msnm:.0f}"
                f"[{self.gate.h0_fuente}]  [{self.segundos:.1f}s]{marca}")


# --------------------------------------------------------------------------
# Procesamiento de un volumen
# --------------------------------------------------------------------------

def procesar(ruta: Path,
             timestamp: datetime,
             cfg,
             serie_h0,
             est: estado_mod.Estado,
             salidas: Salidas) -> Resultado:
    """Un volumen, punta a punta. Muta `est` con el nuevo estado del tracking."""
    import time as _time
    t_inicio = _time.time()

    h0_msnm, h0_fuente = gate.resolver_h0(timestamp, serie_h0)
    v = grilla_mod.leer(ruta, timestamp, cfg, h0_msnm)
    r_gate = gate.evaluar(v.vol, h0_msnm, h0_fuente, cfg.referencia_z, v.alt0)

    res = Resultado(timestamp=timestamp, archivo=ruta.name, gate=r_gate)

    if not r_gate.pasa:
        # Descartar el volumen es EXACTAMENTE equivalente a procesarlo y no
        # obtener ningun sistema convectivo: por la garantia del gate, ninguno
        # habria pasado C2. Por eso el estado se actualiza igual que en un frame
        # vacio (previous = []) en vez de dejarse intacto. Si se dejara intacto,
        # el proximo volumen que pase asociaria contra sistemas viejos.
        est.previous = []
        est.ultimo_timestamp = timestamp
        res.segundos = _time.time() - t_inicio
        return res

    detector = detectors.GreaterThan(cfg.dbz_threshold,
                                     km2_a_deg2(cfg.min_area_km2, v.lat0))
    current = detector.detect(v.raster)
    res.detectados = len(current)

    clasificar_sistemas(current, v, cfg, h0_msnm, h0_fuente)
    current = fc.filtrar_convectivos(current)
    res.convectivos = len(current)

    for s in current:
        s.timestamp = timestamp

    # zonal_stats dentro de cada poligono. rasterOut=True adjunta el recorte del
    # raster a cada sistema, que es lo que va a la columna `raster` de la base.
    descriptors.StatisticalDescriptor(stats=STAT_ATTRS, rasterOut=True).describe(
        v.raster, current)

    # Los atributos de movimiento solo se pueden calcular con un frame previo,
    # pero las columnas tienen que existir igual para que el INSERT no falle.
    for s in current:
        s.addAtributes(MOVEMENT_ATTRS)

    # ── Tracking ─────────────────────────────────────────────────────────────
    previous = estado_mod.deserializar_sistemas(est.previous)

    # Si paso demasiado tiempo desde el ultimo volumen procesado, asociar seria
    # inventar trayectorias: a 15 m/s un sistema recorre 18 km en 20 min y ya no
    # solapa consigo mismo. Se arranca de cero.
    hueco_min = None
    if est.ultimo_timestamp is not None:
        hueco_min = (timestamp - est.ultimo_timestamp).total_seconds() / 60.0
        if hueco_min > cfg.max_gap_tracking_min:
            previous = []
            res.reinicio_tracking = True

    historia = estado_mod.historia_a_deques(est.historia, cfg.velocity_history_len)
    matched = []

    if previous:
        trackers.OverlapAreaTracker(previous, strategy=STRATEGIES[cfg.strategy](cfg)).track(current)
        matched = [s for s in current
                   if s.relationships
                   and (s.timestamp - s.relationships[0].timestamp).total_seconds() > 0]

        if matched:
            descriptors.NormalizedAreaExpansionDescriptor().describe(previous, matched)
            descriptors.MovementDescriptor().describe(previous, matched)

            # Sin historial se pasa uno vacio: misma traslacion de 2 puntos que
            # forecasters.Conservative, pero con la escala corregida.
            if cfg.use_history_velocity:
                actualizar_historia(historia, matched)
                aplicar_velocidad_historia(historia, matched)
            forecasts = pronosticar_con_historia(
                matched, historia if cfg.use_history_velocity else {},
                intervals=list(cfg.forecast_intervals),
                apply_scale=cfg.apply_scale,
                escala_min=cfg.forecast_scale_min,
                escala_max=cfg.forecast_scale_max)

            for lead, fsystems in forecasts.items():
                if not fsystems:
                    continue
                for s in fsystems:
                    s.timestamp = timestamp + timedelta(minutes=lead)
                salidas.forecasts[lead].output(fsystems)
                res.forecasts += len(fsystems)

    res.rastreados = len(matched)
    salidas.systems.output(current)

    # ── Nuevo estado ─────────────────────────────────────────────────────────
    est.previous = estado_mod.serializar_sistemas(current)
    est.ultimo_timestamp = timestamp
    vivos = {s.name for s in current}
    est.historia = estado_mod.podar_historia(
        estado_mod.deques_a_historia(historia), vivos)

    res.segundos = _time.time() - t_inicio
    return res
