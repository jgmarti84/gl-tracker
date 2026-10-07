#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tracking operativo de sistemas convectivos con gate C2 alimentado por WRF.

La entrada es la grilla de radar YA INTERPOLADA: el grillado lo hace otro paso.

    entra la grilla interpolada
              |
    H0 de la ultima corrida del WRF en Cordoba capital
              |
    C2 sobre TODO el volumen: hay algun tope de eco sobre H0 + margen?
              |
        si  -> deteccion + clasificacion + tracking + forecast -> base de datos
        no  -> se descarta el volumen y se espera la siguiente imagen

Uso:

    python operativo.py                      # demonio: espera volumenes nuevos
    python operativo.py --una-vez            # procesa lo pendiente y sale
    python operativo.py --reprocesar 20      # los ultimos 20 volumenes, estado nuevo
    python operativo.py --solo-gate          # evalua el gate y no corre TATHU
    python operativo.py --desde-cero         # ignora el estado guardado

Se detiene limpio con Ctrl-C o SIGTERM: termina el volumen en curso, guarda el
estado y sale. Matarlo a mitad de un volumen no corrompe nada — el estado se
escribe de forma atomica y la base commitea por volumen — pero se pierde ese
volumen, que se reprocesa al arrancar de nuevo.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# El paquete se ejecuta como scripts sueltos, no como paquete instalado: los
# modulos se importan por nombre y para eso su directorio tiene que estar en
# sys.path. config.py agrega ademas el directorio padre, donde vive
# filtro_convectivo.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as config_mod          # noqa: E402
import estado as estado_mod          # noqa: E402
import gate                          # noqa: E402
import pipeline                      # noqa: E402
import grilla as grilla_mod        # noqa: E402

_PARAR = False


def _manejar_senal(signum, frame):
    global _PARAR
    _PARAR = True
    print(f"\n  senal {signum} recibida: se termina el volumen en curso y se guarda el estado.",
          flush=True)


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


# --------------------------------------------------------------------------

def procesar_pendientes(cfg, est, salidas, solo_gate=False, limite=None) -> int:
    """Procesa los volumenes pendientes en orden. Devuelve cuantos proceso.

    En orden cronologico y no solo el ultimo: si el proceso estuvo caido un
    rato, recorrerlos reconstruye las trayectorias en vez de saltar al presente
    y perder la continuidad. `limite` acota el backlog cuando la caida fue larga
    y no vale la pena reconstruir horas de historia.
    """
    pendientes = grilla_mod.descubrir_pendientes(cfg, est.ultimo_timestamp)
    if limite is not None and len(pendientes) > limite:
        omitidos = len(pendientes) - limite
        log(f"backlog de {len(pendientes)} volumenes: se omiten los {omitidos} mas viejos")
        pendientes = pendientes[-limite:]

    procesados = 0
    for ts, ruta in pendientes:
        if _PARAR:
            break
        try:
            if solo_gate:
                res = evaluar_solo_gate(ruta, ts, cfg)
            else:
                serie = gate.serie_h0_actual(cfg, cfg.h0_lat, cfg.h0_lon)
                res = pipeline.procesar(ruta, ts, cfg, serie, est, salidas)
                estado_mod.guardar(cfg.estado_path, est)
            log(res.linea())
            procesados += 1
        except Exception as exc:
            # Un volumen corrupto o incompleto no puede tumbar el operativo. Se
            # loguea y se sigue; el estado no se toca, asi que el proximo
            # volumen bueno asocia contra el ultimo frame valido.
            log(f"{ts:%Y-%m-%d %H:%M:%S}  ERROR {ruta.name}: {exc!r}")
    return procesados


def evaluar_solo_gate(ruta: Path, ts: datetime, cfg) -> pipeline.Resultado:
    """Corre hasta el gate y no sigue. Para medir cuanto descartaria."""
    serie = gate.serie_h0_actual(cfg, cfg.h0_lat, cfg.h0_lon)
    h0_msnm, fuente = gate.resolver_h0(ts, serie)
    t0 = time.time()
    v = grilla_mod.leer(ruta, ts, cfg, h0_msnm)
    r = gate.evaluar(v.vol, h0_msnm, fuente, cfg.referencia_z, v.alt0)
    res = pipeline.Resultado(timestamp=ts, archivo=ruta.name, gate=r)
    res.segundos = time.time() - t0
    if r.pasa:
        # En modo solo-gate no hay deteccion, asi que la linea normal mentiria.
        res.gate = r
        return _ResultadoGateSolo(res)
    return res


class _ResultadoGateSolo:
    """Envuelve un Resultado para que la linea de log no prometa deteccion."""

    def __init__(self, res):
        self._res = res

    def __getattr__(self, k):
        return getattr(self._res, k)

    def linea(self) -> str:
        return (f"{self._res.timestamp:%Y-%m-%d %H:%M:%S}  {self._res.gate}  "
                f"[{self._res.segundos:.1f}s]")


# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--una-vez", action="store_true",
                   help="procesa lo pendiente y sale (para cron)")
    p.add_argument("--reprocesar", type=int, metavar="N",
                   help="reprocesa los ultimos N volumenes desde cero")
    p.add_argument("--solo-gate", action="store_true",
                   help="evalua el gate y no corre TATHU; para medir el descarte")
    p.add_argument("--desde-cero", action="store_true",
                   help="ignora el estado guardado y arranca el tracking de nuevo")
    p.add_argument("--grilla-raiz", type=Path, help="directorio de grillas interpoladas")
    p.add_argument("--wrfout-raiz", type=Path, help="raiz de los wrfout")
    p.add_argument("--out", type=Path, help="directorio de salida")
    p.add_argument("--max-backlog", type=int, default=None,
                   help="maximo de volumenes atrasados a recuperar por vuelta")
    args = p.parse_args()

    cfg = config_mod.Config()
    if args.grilla_raiz:
        cfg.grilla_raiz = args.grilla_raiz
    if args.wrfout_raiz:
        cfg.wrfout_raiz = args.wrfout_raiz
    if args.out:
        cfg.out_dir = args.out

    signal.signal(signal.SIGINT, _manejar_senal)
    signal.signal(signal.SIGTERM, _manejar_senal)

    log(f"grillas: {cfg.grilla_raiz}  ({cfg.grilla_glob})")
    log(f"wrfout : {cfg.wrfout_raiz}")
    log(f"salida : {cfg.out_dir}")
    log(f"H0 en  : {cfg.h0_lat:.4f}, {cfg.h0_lon:.4f}  (columna del WRF)")

    serie = gate.serie_h0_actual(cfg, cfg.h0_lat, cfg.h0_lon)
    if serie is None:
        log("AVISO: no hay corrida de WRF utilizable. Se filtra con la climatologia.")
    else:
        log(f"H0     : {serie.resumen()}")

    est = estado_mod.Estado() if (args.desde_cero or args.reprocesar) \
        else estado_mod.cargar(cfg.estado_path)
    if est.ultimo_timestamp:
        log(f"estado: reanuda desde {est.ultimo_timestamp:%Y-%m-%d %H:%M:%S}Z "
            f"con {len(est.previous)} sistema(s) vivos")

    if args.reprocesar:
        todos = grilla_mod.listar(cfg)
        if not todos:
            log("no hay volumenes")
            return 1
        est.ultimo_timestamp = None
        recorte = todos[-args.reprocesar:]
        est.ultimo_timestamp = None if len(recorte) == len(todos) \
            else todos[-args.reprocesar - 1][0]

    salidas = None if args.solo_gate else pipeline.Salidas(cfg)

    if args.solo_gate:
        log("modo solo-gate: no se corre TATHU ni se escribe la base")

    # ── Una pasada ───────────────────────────────────────────────────────────
    if args.una_vez or args.reprocesar or args.solo_gate:
        n = procesar_pendientes(cfg, est, salidas, args.solo_gate, args.max_backlog)
        log(f"listo: {n} volumen(es) procesado(s)")
        return 0

    # ── Demonio ──────────────────────────────────────────────────────────────
    log(f"esperando volumenes nuevos (sondeo cada {cfg.intervalo_sondeo_s:.0f}s). "
        f"Ctrl-C para salir.")
    while not _PARAR:
        try:
            procesar_pendientes(cfg, est, salidas, limite=args.max_backlog)
        except FileNotFoundError as exc:
            # El directorio de radar puede desaparecer temporalmente (montaje
            # NFS que se cae). No es motivo para terminar el demonio.
            log(f"AVISO: {exc}")
        for _ in range(int(cfg.intervalo_sondeo_s)):
            if _PARAR:
                break
            time.sleep(1)

    estado_mod.guardar(cfg.estado_path, est)
    log("estado guardado. Fin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
