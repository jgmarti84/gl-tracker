#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Propiedades del pronostico que no se pueden romper.

El forecaster de TATHU aplica la tasa de crecimiento de AREA a cada lado del
poligono: el area pronosticada crece al cuadrado, y con tasas negativas el
factor cruza cero y el poligono se espeja. `pronosticar_con_historia` corrige
eso (raiz + recorte); este archivo lo fija por escrito.

    python test_forecast.py
"""

from __future__ import annotations

import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gate                        # noqa: F401  bootstrap de sys.path, como en test_gate
import pipeline                    # noqa: E402
from osgeo import ogr              # noqa: E402
from tathu.tracking.system import ConvectiveSystem, LifeCycleEvent  # noqa: E402

T0 = datetime(2026, 10, 22, 15, 17)
DT = timedelta(minutes=9)
INTERVALOS = [9, 18, 27, 36, 45, 54, 63]


def cuadrado(cx, cy, lado):
    h = lado / 2
    return ogr.CreateGeometryFromWkt(
        f"POLYGON(({cx-h} {cy-h},{cx+h} {cy-h},{cx+h} {cy+h},{cx-h} {cy+h},{cx-h} {cy-h}))")


def sistema(geom, ts, nombre="trk", previo=None):
    s = ConvectiveSystem(geom)
    s.name = nombre
    s.timestamp = ts
    s.event = LifeCycleEvent.CONTINUITY
    s.relationships = [previo] if previo is not None else []
    return s


def par(lado_previo, lado_actual):
    """Sistema actual con un previo 9 min antes, desplazado 0.05 deg al este."""
    previo = sistema(cuadrado(-63.0, -31.0, lado_previo), T0)
    actual = sistema(cuadrado(-62.95, -31.0, lado_actual), T0 + DT, previo=previo)
    return actual


def pronostico(actual, historia=None, apply_scale=True):
    historia = historia if historia is not None else defaultdict(lambda: deque(maxlen=4))
    pipeline.actualizar_historia(historia, [actual])
    return pipeline.pronosticar_con_historia(
        [actual], historia, INTERVALOS, apply_scale=apply_scale)


def test_factor_escala():
    f = pipeline.factor_escala
    assert f(0.0, 63, 0.5, 2.0) == 1.0
    # area x2 -> cada lado x raiz(2), no x2
    assert abs(f(1.0 / 63, 63, 0.5, 2.0) - 2 ** 0.5) < 1e-9
    # crecimiento enorme -> recortado
    assert f(0.08, 63, 0.5, 2.0) == 2.0
    # decrecimiento que en TATHU cruzaria cero -> recortado, nunca negativo
    assert f(-0.03, 63, 0.5, 2.0) == 0.5


def test_crecimiento_acotado():
    """Area +70% en 9 min (el caso de RMA17): a +63 min, area <= 4x."""
    actual = par(0.10, 0.10 * 1.7 ** 0.5)
    fc = pronostico(actual)
    a0 = actual.geom.GetArea()
    areas = [fc[t][0].geom.GetArea() / a0 for t in INTERVALOS]
    assert all(b >= a for a, b in zip(areas, areas[1:])), areas   # monotona
    assert areas[-1] <= 4.0 + 1e-9, areas
    assert areas[0] > 1.0, areas


def test_decrecimiento_no_espeja():
    """Area -40% en 9 min. TATHU da factor 1 - 0.044*63 = -1.8 a +63 min: el
    poligono sale espejado y con 3.2x el area, o sea un sistema que se achica
    se pronostica MAS grande. Aca el area queda en [0.25x, 1x]."""
    actual = par(0.10, 0.10 * 0.6 ** 0.5)
    fc = pronostico(actual)
    a0 = actual.geom.GetArea()
    for t in INTERVALOS:
        r = fc[t][0].geom.GetArea() / a0
        assert 0.25 - 1e-9 <= r <= 1.0 + 1e-9, (t, r)


def test_sin_escala():
    actual = par(0.10, 0.15)
    fc = pronostico(actual, apply_scale=False)
    a0 = actual.geom.GetArea()
    assert all(abs(fc[t][0].geom.GetArea() - a0) < 1e-12 for t in INTERVALOS)


def test_historia_vieja():
    """Un estado guardado antes de este cambio trae tuplas de 3 elementos."""
    historia = defaultdict(lambda: deque(maxlen=4))
    historia["trk"].append((T0 - DT, -63.05, -31.0))
    assert pipeline.tasa_crecimiento(historia, "trk") is None
    fc = pronostico(par(0.10, 0.12), historia=historia)
    assert len(fc[63]) == 1


if __name__ == "__main__":
    for nombre, fn in list(globals().items()):
        if nombre.startswith("test_") and callable(fn):
            fn()
    print("test_forecast: OK")
