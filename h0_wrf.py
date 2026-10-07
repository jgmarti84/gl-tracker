#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Altura de la isoterma de 0 grados en un punto, desde la ultima corrida del WRF.

Reemplaza a `h0_climatologico()` del pipeline de radar: donde el ciclo anual
sinusoidal daba una media de la epoca, esto da el valor pronosticado para el
dia y la hora del volumen.

La diferencia no es cosmetica. Sobre el wrfout del 4-mar-2022 en el punto de
RMA1 la climatologia da 4294 m s.n.m. y el WRF 5042 m: 748 m de diferencia, en
la direccion que anticipaba el comentario del notebook ("un dia convectivo
suele estar en el sector calido, con H0 por encima de la media de su epoca").
Sobre un margen C2 de 1500 m, 748 m no es un detalle.

Uso como modulo:

    from h0_wrf import serie_h0
    serie = serie_h0(lat=-31.44133, lon=-64.19199)   # busca la ultima corrida
    h0 = serie.en(timestamp_del_volumen)             # metros s.n.m.

Uso como script (para inspeccionar o precalentar el cache):

    python h0_wrf.py --lat -31.44133 --lon -64.19199
    python h0_wrf.py --raiz /ruta/a/wrfouts --lat ... --lon ... --json

Nota de dependencias: lee los campos crudos del netCDF en vez de usar
wrf-python. No es por purismo: el venv donde corre el pipeline de radar
(Tracking/venv) tiene netCDF4 pero no wrf-python, y para una sola columna
leer T/P/PB/PH/PHB directo es ademas mucho mas barato que pedirle a
`wrf.getvar` que compute el dominio entero para despues tirar el 99.99%.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from netCDF4 import Dataset

# --------------------------------------------------------------------------
# Constantes
# --------------------------------------------------------------------------

# Raiz de los wrfout del operativo. Misma convencion que usan extraer.py y
# wrfstac. Se sobreescribe con WRFOUT_RAIZ o con --raiz.
WRFOUT_RAIZ = Path(os.getenv("WRFOUT_RAIZ", "/home/datos/wrfdatos/wrfout"))

# wrfout_A_d01_2026-08-06_06:00:00.nc      <- convencion del operativo
# wrfout_A_d01_2022-03-04_18_00_00.nc      <- variante con guiones bajos
RE_WRFOUT = re.compile(
    r"^wrfout_(?P<param>[A-Za-z0-9]+)_d(?P<dominio>\d+)_"
    r"(?P<fecha>\d{4}-\d{2}-\d{2})_"
    r"(?P<hora>\d{2})[:_](?P<minuto>\d{2})[:_](?P<segundo>\d{2})\.nc$"
)

# Orden de preferencia de parametrizaciones, el mismo que recorre el script de
# meteogramas del OHMC. La primera disponible gana; None acepta cualquiera en
# orden alfabetico.
PARAMS_PREFERENCIA = ("A", "B", "C", "D")

CERO_C = 273.15          # K
GRAVEDAD = 9.81          # m/s2
P_REF = 100000.0         # Pa, presion de referencia de theta
R_SOBRE_CP = 0.2854      # Rd/cp para aire seco

CACHE_DIR = Path(__file__).resolve().parent / "out" / "h0_cache"

# Tolerancia al extrapolar fuera del rango temporal de la corrida. Si el
# volumen de radar cae mas lejos que esto del primer/ultimo tiempo del wrfout,
# se considera que la corrida no lo cubre.
TOLERANCIA_EXTRAPOLACION = timedelta(hours=1)


# --------------------------------------------------------------------------
# Descubrimiento de la ultima corrida
# --------------------------------------------------------------------------

def candidatos(raiz: Path):
    """Todos los wrfout bajo la raiz, en {YYYY_MM}/ o sueltos."""
    if not raiz.is_dir():
        raise FileNotFoundError(
            f"No existe el directorio de wrfout: {raiz}. "
            f"Indicalo con --raiz o con la variable de entorno WRFOUT_RAIZ."
        )
    directorios = [raiz] + [d for d in raiz.iterdir() if d.is_dir()]
    for directorio in directorios:
        for ruta in directorio.iterdir():
            m = RE_WRFOUT.match(ruta.name)
            if m:
                yield ruta, m


def esta_cerrado(ruta: Path, edad_minima: float) -> bool:
    """True si hace rato que nadie escribe el archivo.

    El WRF va escribiendo el wrfout mientras corre. Leerlo a mitad de escritura
    no da error: da menos horas de las que corresponde, sin ningun aviso. Para
    H0 el efecto seria peor que un meteograma corto — el pipeline de radar
    pediria una hora que el archivo todavia no tiene y caeria al respaldo
    climatologico sin que nadie se entere.
    """
    return (time.time() - ruta.stat().st_mtime) >= edad_minima


def descubrir_ultima_corrida(raiz: Path = WRFOUT_RAIZ,
                             params: tuple[str, ...] | str | None = PARAMS_PREFERENCIA,
                             dominio: int | None = 1,
                             edad_minima: float = 300.0) -> tuple[str, list[Path]]:
    """(id_corrida, archivos) de la corrida mas reciente que este utilizable.

    Dos niveles de respaldo, en este orden:

    1. Dentro de una misma corrida, se prueban las parametrizaciones en el ORDEN
       DE PREFERENCIA de `params` y se toma la primera disponible. Es la misma
       logica del script de meteogramas del OHMC, que recorre la lista de
       parametrizaciones y corta con `break` en la primera que encuentra.

       Esto importa: ordenar las claves alfabeticamente y quedarse con la ultima
       elegiria la parametrizacion D sobre la A, que es lo contrario de lo que
       hace el operativo.

    2. Si ninguna parametrizacion de la corrida mas nueva esta completa, se pasa
       a la corrida anterior. Para el filtro convectivo un H0 de hace 12 h es
       infinitamente mejor que ninguno.
    """
    if isinstance(params, str):
        params = (params,)

    # {(fecha, hora): {param: [rutas]}}
    corridas: dict[tuple, dict[str, list[Path]]] = {}
    for ruta, m in candidatos(raiz):
        prm = m.group("param")
        if params and prm not in params:
            continue
        if dominio is not None and int(m.group("dominio")) != dominio:
            continue
        corridas.setdefault((m.group("fecha"), m.group("hora")), {}) \
                .setdefault(prm, []).append(ruta)

    if not corridas:
        detalle = f" (params={list(params)}, d{dominio:02d})" if params else ""
        raise FileNotFoundError(f"No se encontro ningun wrfout en {raiz}{detalle}")

    # Orden de preferencia: el declarado en `params`; si es None, alfabetico.
    prioridad = {p: i for i, p in enumerate(params)} if params else {}

    for fecha, hora in sorted(corridas, reverse=True):
        disponibles = corridas[(fecha, hora)]
        for prm in sorted(disponibles, key=lambda p: prioridad.get(p, len(prioridad))):
            archivos = sorted(disponibles[prm])
            if all(esta_cerrado(r, edad_minima) for r in archivos):
                dom = f"{dominio:02d}" if dominio is not None else "??"
                return f"{fecha}_{hora}_{prm}_d{dom}", archivos

    raise FileNotFoundError(
        f"Todas las corridas en {raiz} se modificaron hace menos de {edad_minima:.0f} s. "
        f"No se lee ninguna para no tomar un wrfout a medio escribir."
    )


# --------------------------------------------------------------------------
# Lectura de la columna
# --------------------------------------------------------------------------

def leer_tiempos(ds) -> list[datetime]:
    """Times del wrfout -> lista de datetime UTC."""
    tiempos = []
    for fila in ds.variables["Times"][:]:
        texto = b"".join(fila).decode() if isinstance(fila[0], bytes) else "".join(fila)
        tiempos.append(
            datetime.strptime(texto.strip(), "%Y-%m-%d_%H:%M:%S").replace(tzinfo=timezone.utc)
        )
    return tiempos


def celda_mas_cercana(lats, lons, lat: float, lon: float) -> tuple[int, int, float]:
    """Indice (j, i) de la celda de grilla mas cercana, y su distancia en km.

    Distancia en el plano lat/lon corrigiendo el achatamiento por coseno de la
    latitud. A 4 km de resolucion la aproximacion es holgadamente suficiente.
    """
    dlat = lats - lat
    dlon = (lons - lon) * np.cos(np.radians(lat))
    d2 = dlat ** 2 + dlon ** 2
    j, i = np.unravel_index(np.argmin(d2), d2.shape)
    return int(j), int(i), float(np.sqrt(d2[j, i])) * 111.32


def columna_t_z(ds, j: int, i: int) -> tuple[np.ndarray, np.ndarray]:
    """Temperatura [K] y altura [m s.n.m.] de la columna (j, i), todos los tiempos.

    WRF no guarda ni temperatura ni altura: hay que reconstruirlas.

      * T  es la PERTURBACION de theta respecto de 300 K -> theta = T + 300
      * la presion es P (perturbacion) + PB (estado base)
      * tk = theta * (p / p0) ** (Rd/cp)
      * el geopotencial (PH + PHB) esta en niveles ESCALONADOS (nz+1); se
        promedia a los puntos de masa para que coincida con la grilla de T.

    Devuelve arrays (ntime, nz).
    """
    theta = np.asarray(ds.variables["T"][:, :, j, i], dtype="float64") + 300.0
    presion = (np.asarray(ds.variables["P"][:, :, j, i], dtype="float64")
               + np.asarray(ds.variables["PB"][:, :, j, i], dtype="float64"))
    tk = theta * (presion / P_REF) ** R_SOBRE_CP

    geo = (np.asarray(ds.variables["PH"][:, :, j, i], dtype="float64")
           + np.asarray(ds.variables["PHB"][:, :, j, i], dtype="float64")) / GRAVEDAD
    z = 0.5 * (geo[:, :-1] + geo[:, 1:])          # msnm, sin restar el terreno
    return tk, z


def altura_isoterma_0(tk: np.ndarray, z: np.ndarray) -> float:
    """Altura [m s.n.m.] del primer cruce de 0 grados subiendo desde el suelo.

    Se toma el cruce MAS BAJO a proposito. Con una inversion puede haber varios
    (aire frio en superficie, capa calida encima, frio de nuevo arriba) y el
    nivel de fusion relevante para la banda brillante y para el tope de eco es
    el de abajo.

    Devuelve NaN si toda la columna esta bajo cero, que sobre Cordoba solo
    ocurriria con un error de lectura.
    """
    bajo_cero = np.flatnonzero(tk < CERO_C)
    if bajo_cero.size == 0:
        return float("nan")

    k = int(bajo_cero[0])
    if k == 0:
        # La superficie ya esta bajo cero: la isoterma esta en el terreno o
        # debajo. Se devuelve el primer nivel, no se extrapola hacia abajo.
        return float(z[0])

    t_arr, t_ab = tk[k], tk[k - 1]
    z_arr, z_ab = z[k], z[k - 1]
    if t_arr == t_ab:                              # capa isoterma exacta
        return float(z_ab)
    return float(z_ab + (CERO_C - t_ab) * (z_arr - z_ab) / (t_arr - t_ab))


# --------------------------------------------------------------------------
# Serie temporal de H0
# --------------------------------------------------------------------------

@dataclass
class SerieH0:
    """H0 pronosticado en un punto, con su procedencia.

    `en(timestamp)` es la unica interfaz que necesita el pipeline de radar:
    recibe la fecha/hora del volumen y devuelve metros sobre el nivel del mar.
    """
    corrida: str
    tiempos: list[datetime]           # UTC, ordenados
    h0_msnm: np.ndarray               # (ntime,)
    punto: dict

    def en(self, timestamp: datetime, interpolar: bool = True) -> float:
        """H0 [m s.n.m.] a la hora pedida.

        El WRF es horario y RMA1 cicla cada ~9 min, asi que casi siempre hay
        que interpolar. H0 se mueve despacio (decimas de metro por minuto), asi
        que la interpolacion lineal sobra; `interpolar=False` toma la hora mas
        cercana y da practicamente lo mismo.

        Levanta ValueError si la corrida no cubre esa hora. Es deliberado: el
        que llama tiene que decidir si cae al respaldo climatologico, y esa
        decision no puede quedar escondida acá.
        """
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        t0, t1 = self.tiempos[0], self.tiempos[-1]
        if not (t0 - TOLERANCIA_EXTRAPOLACION <= timestamp <= t1 + TOLERANCIA_EXTRAPOLACION):
            raise ValueError(
                f"La corrida {self.corrida} cubre {t0:%Y-%m-%d %H:%M}Z a {t1:%Y-%m-%d %H:%M}Z; "
                f"se pidio {timestamp:%Y-%m-%d %H:%M}Z."
            )

        segundos = np.array([(t - t0).total_seconds() for t in self.tiempos])
        objetivo = (timestamp - t0).total_seconds()
        if not interpolar:
            return float(self.h0_msnm[int(np.argmin(np.abs(segundos - objetivo)))])
        # np.interp satura en los extremos, que es justo lo que queremos dentro
        # de la ventana de tolerancia ya validada arriba.
        return float(np.interp(objetivo, segundos, self.h0_msnm))

    def resumen(self) -> str:
        finitos = self.h0_msnm[np.isfinite(self.h0_msnm)]
        rango = f"{finitos.min():.0f}-{finitos.max():.0f} m" if finitos.size else "sin datos"
        return (f"corrida {self.corrida} | {len(self.tiempos)} tiempos "
                f"({self.tiempos[0]:%Y-%m-%d %H:%M}Z a {self.tiempos[-1]:%Y-%m-%d %H:%M}Z) | "
                f"H0 {rango} | celda a {self.punto['distancia_km']:.1f} km del punto")


def _extraer(archivos: list[Path], corrida: str, lat: float, lon: float) -> SerieH0:
    tiempos: list[datetime] = []
    h0: list[float] = []
    punto = None

    for ruta in sorted(archivos):
        with Dataset(ruta) as ds:
            faltan = [v for v in ("T", "P", "PB", "PH", "PHB", "XLAT", "XLONG", "HGT")
                      if v not in ds.variables]
            if faltan:
                raise KeyError(f"{ruta.name}: faltan variables {', '.join(faltan)}")

            lats = ds.variables["XLAT"][0]
            lons = ds.variables["XLONG"][0]
            j, i, dist_km = celda_mas_cercana(lats, lons, lat, lon)

            if punto is None:
                punto = {
                    "lat_pedida": lat,
                    "lon_pedida": lon,
                    "lat": round(float(lats[j, i]), 4),
                    "lon": round(float(lons[j, i]), 4),
                    "terreno_msnm": round(float(ds.variables["HGT"][0, j, i]), 1),
                    "distancia_km": round(dist_km, 2),
                    "j": j,
                    "i": i,
                }

            tk, z = columna_t_z(ds, j, i)
            tiempos.extend(leer_tiempos(ds))
            h0.extend(altura_isoterma_0(tk[t], z[t]) for t in range(tk.shape[0]))

    orden = np.argsort(tiempos)
    return SerieH0(
        corrida=corrida,
        tiempos=[tiempos[k] for k in orden],
        h0_msnm=np.asarray(h0, dtype="float64")[orden],
        punto=punto,
    )


def serie_h0(lat: float,
             lon: float,
             raiz: Path = WRFOUT_RAIZ,
             params: tuple[str, ...] | str | None = PARAMS_PREFERENCIA,
             dominio: int | None = 1,
             edad_minima: float = 300.0,
             usar_cache: bool = True) -> SerieH0:
    """Serie de H0 en un punto, desde la ultima corrida completa del WRF.

    El cache va por (corrida, punto): la corrida cambia dos veces al dia y el
    punto del radar no cambia nunca, asi que en operativo se lee el wrfout una
    vez cada 12 h y los ~80 volumenes siguientes salen del pickle.
    """
    corrida, archivos = descubrir_ultima_corrida(raiz, params, dominio, edad_minima)

    cache = CACHE_DIR / f"h0_{corrida}_{lat:.4f}_{lon:.4f}.pkl"
    if usar_cache and cache.is_file():
        try:
            with open(cache, "rb") as f:
                return pickle.load(f)
        except (pickle.PickleError, OSError, AttributeError):
            # Cache corrupto o escrito por otra version del modulo: se recalcula.
            pass

    serie = _extraer(archivos, corrida, lat, lon)

    if usar_cache:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(cache, "wb") as f:
                pickle.dump(serie, f, protocol=pickle.HIGHEST_PROTOCOL)
        except OSError:
            pass          # sin cache se recalcula, no es motivo para abortar

    return serie


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Altura de la isoterma de 0 C en un punto, desde la ultima corrida del WRF.")
    p.add_argument("--lat", type=float, required=True, help="latitud del punto (grados, negativo al sur)")
    p.add_argument("--lon", type=float, required=True, help="longitud del punto (grados, negativo al oeste)")
    p.add_argument("--raiz", type=Path, default=WRFOUT_RAIZ, help=f"raiz de los wrfout (default {WRFOUT_RAIZ})")
    p.add_argument("--params", default=",".join(PARAMS_PREFERENCIA),
                   help="parametrizaciones en orden de preferencia, separadas por coma "
                        f"(default {','.join(PARAMS_PREFERENCIA)})")
    p.add_argument("--dominio", type=int, default=1, help="dominio a usar (default 1)")
    p.add_argument("--edad-minima", type=float, default=300.0,
                   help="segundos que un wrfout debe llevar sin modificarse (default 300)")
    p.add_argument("--sin-cache", action="store_true", help="ignora y no escribe el cache")
    p.add_argument("--hora", default=None, help="hora UTC puntual a consultar, ISO 8601")
    p.add_argument("--json", action="store_true", help="salida como JSON")
    args = p.parse_args()

    try:
        serie = serie_h0(args.lat, args.lon, args.raiz,
                         tuple(x.strip() for x in args.params.split(",") if x.strip()),
                         args.dominio, args.edad_minima, usar_cache=not args.sin_cache)
    except (FileNotFoundError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.hora:
        ts = datetime.fromisoformat(args.hora)
        try:
            valor = serie.en(ts)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"corrida": serie.corrida, "hora": ts.isoformat(),
                              "h0_msnm": round(valor, 1), "punto": serie.punto}, indent=2))
        else:
            print(f"{serie.resumen()}\n  {ts:%Y-%m-%d %H:%M}Z -> H0 = {valor:.0f} m s.n.m.")
        return 0

    if args.json:
        print(json.dumps({
            "corrida": serie.corrida,
            "punto": serie.punto,
            "series": [{"hora": t.isoformat(), "h0_msnm": None if not np.isfinite(v) else round(v, 1)}
                       for t, v in zip(serie.tiempos, serie.h0_msnm)],
        }, indent=2))
    else:
        print(serie.resumen())
        print(f"  terreno del modelo: {serie.punto['terreno_msnm']:.0f} m s.n.m.")
        for t, v in zip(serie.tiempos, serie.h0_msnm):
            print(f"  {t:%Y-%m-%d %H:%M}Z   {v:8.0f} m s.n.m.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
