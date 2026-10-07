#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Propiedades que el gate no puede romper.

El gate descarta volumenes enteros sin detectar nada. Toda la justificacion de
que eso es seguro descansa en una desigualdad — el maximo global es >= el
maximo sobre cualquier objeto — y en dos detalles de implementacion que es facil
romper sin darse cuenta (hardcodear un umbral, sacar el recorte de radio). Este
archivo fija esas propiedades por escrito.

    python test_gate.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

import gate                        # importar gate primero: hace el bootstrap de sys.path
import filtro_convectivo as fc     # noqa: E402

H0_MSNM = 4500.0
FUENTE = "test"


def _volumen(altura_tope_m, z_m, x_km, y_km, cfg, jj=30, ii=30, radio=4):
    """Un volumen sintetico con una unica torre de 50 dBZ hasta cierta altura.

    El assert no es decorativo. Con `jj - radio` negativo, numpy interpreta el
    slice desde el final del eje y `dbzh[:, -1:7, ...]` queda VACIO: no se
    construye ninguna torre y el volumen sale sin eco. Un test que compara
    contra un volumen vacio pasa siempre, por el motivo equivocado — que es
    exactamente lo que pasaba en la comprobacion de r_max_km.
    """
    nz, ny, nx = len(z_m), len(y_km), len(x_km)
    if not (radio <= jj <= ny - radio and radio <= ii <= nx - radio):
        raise AssertionError(
            f"torre en (jj={jj}, ii={ii}) con radio={radio} se sale de la grilla "
            f"({ny}x{nx}); el slice quedaria vacio y el test no probaria nada")

    dbzh = np.full((nz, ny, nx), np.nan, dtype="float32")
    rhohv = np.full((nz, ny, nx), 0.99, dtype="float32")
    k = int(np.searchsorted(z_m, altura_tope_m, side="right"))
    dbzh[:k, jj - radio:jj + radio, ii - radio:ii + radio] = 50.0

    if not np.isfinite(dbzh).any():
        raise AssertionError("el volumen sintetico salio sin ningun eco")
    return fc.preparar_volumen(dbzh, rhohv, x_km, y_km, z_m, H0_MSNM, cfg), rhohv


def main() -> int:
    rng = np.random.default_rng(0)
    z_m = np.arange(21) * 600.0
    x_km = np.linspace(-30, 30, 60)
    y_km = np.linspace(-30, 30, 60)
    cfg = fc.ConvectiveFilterConfig(r_max_km=150.0)
    umbral = H0_MSNM + cfg.echotop_min_above_h0_m       # referencia msnm

    fallos = []

    # 1. Una torre por encima del umbral enciende el gate; una por debajo, no.
    for altura, esperado in ((umbral + 1200, True), (umbral - 1200, False)):
        vol, _ = _volumen(altura, z_m, x_km, y_km, cfg)
        r = gate.evaluar(vol, H0_MSNM, FUENTE, "msnm")
        if r.pasa is not esperado:
            fallos.append(f"torre a {altura:.0f} m: pasa={r.pasa}, esperado {esperado}")

    # 2. La garantia central. Si el gate da False, C2 tiene que fallar para el
    #    objeto mas grande posible — y por lo tanto para cualquier objeto real,
    #    que es un subconjunto de el.
    for _ in range(80):
        altura = float(rng.uniform(1000, 11000))
        vol, rhohv = _volumen(altura, z_m, x_km, y_km, cfg,
                              jj=int(rng.integers(8, 52)), ii=int(rng.integers(8, 52)))
        if gate.evaluar(vol, H0_MSNM, FUENTE, "msnm").pasa:
            continue
        mask = np.isfinite(vol.echo_top)
        if mask.any() and vol.clasificar(mask, rhohv)["c2_vertical"]:
            fallos.append(f"SOUNDNESS ROTA: gate False pero C2 True (tope {altura:.0f} m)")

    # 3. El recorte a r_max_km: un eco alto fuera del radio no debe encender el
    #    gate. La torre va en (10, 10) -> ~28 km del centro, entera fuera de los
    #    10 km de r_max, y con margen suficiente para que el slice sea valido.
    cfg_chico = fc.ConvectiveFilterConfig(r_max_km=10.0)
    vol, _ = _volumen(umbral + 2000, z_m, x_km, y_km, cfg_chico, jj=10, ii=10)
    # Sin la torre esta comprobacion no distingue nada: hay que verificar que el
    # eco existe y que esta fuera del radio antes de exigir que el gate lo ignore.
    dist_torre = float(np.nanmin(np.where(np.isfinite(vol.echo_top), vol.dist_km, np.nan)))
    if not np.isfinite(vol.echo_top).any():
        fallos.append("la torre de la prueba de r_max_km no se construyo")
    elif dist_torre <= cfg_chico.r_max_km:
        fallos.append(f"la torre quedo dentro de r_max_km ({dist_torre:.1f} km); "
                      f"la prueba no distingue nada")
    elif gate.evaluar(vol, H0_MSNM, FUENTE, "msnm").pasa:
        fallos.append("el gate se encendio por un eco fuera de r_max_km")

    # 4. El gate sigue a cfg, no a constantes propias. Al subir el margen de C2,
    #    una torre que antes pasaba tiene que dejar de pasar.
    cfg_alto = fc.ConvectiveFilterConfig(echotop_min_above_h0_m=6000.0)
    vol, _ = _volumen(umbral + 1200, z_m, x_km, y_km, cfg_alto)
    if gate.evaluar(vol, H0_MSNM, FUENTE, "msnm").pasa:
        fallos.append("el gate ignoro echotop_min_above_h0_m de cfg")

    # 5. Conversion de referencia vertical.
    if gate.h0_para_grilla(4500.0, "radar") != 4500.0 - gate.RMA1_ALTITUD_M:
        fallos.append("h0_para_grilla('radar') no resta la altitud de la antena")
    if gate.h0_para_grilla(4500.0, "msnm") != 4500.0:
        fallos.append("h0_para_grilla('msnm') no debe modificar el valor")
    try:
        gate.h0_para_grilla(4500.0, "sobre_el_mar")
        fallos.append("h0_para_grilla acepto una referencia invalida")
    except ValueError:
        pass

    # 6. No hay modo degradado: resolver_h0 siempre devuelve un numero.
    valor, fuente = gate.resolver_h0(datetime(2026, 1, 1, 17, 30), serie=None)
    if not np.isfinite(valor) or fuente != "climatologia":
        fallos.append(f"resolver_h0 sin serie devolvio ({valor}, {fuente})")

    # 7. Volumen sin ningun eco: el gate descarta, no explota.
    vacio = np.full((21, 60, 60), np.nan, dtype="float32")
    vol = fc.preparar_volumen(vacio, vacio, x_km, y_km, z_m, H0_MSNM, cfg)
    r = gate.evaluar(vol, H0_MSNM, FUENTE, "msnm")
    if r.pasa or r.n_pixeles_pasan != 0:
        fallos.append(f"volumen vacio: {r}")

    for f in fallos:
        print("FALLO:", f)
    print("test_gate: OK" if not fallos else f"test_gate: {len(fallos)} fallo(s)")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
