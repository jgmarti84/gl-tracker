# -*- coding: utf-8 -*-
"""Gate C2 a nivel de volumen: decide si el volumen entra a TATHU.

    entra el volumen grillado (nz, ny, nx)
              |
    H0 de la ultima corrida del WRF en Cordoba capital
              |
    C2 sobre TODO el volumen:  max(tope_de_eco) >= H0 + margen ?
              |
        True  -> el volumen sigue a deteccion y tracking
        False -> se descarta y se espera la siguiente imagen

Por que es correcto
-------------------
C2 por objeto es `max(echo_top[mask_obj]) >= h0 + margen`. El maximo sobre
todos los pixeles es siempre >= el maximo sobre los pixeles de cualquier
objeto. Entonces si el gate da False, C2 falla para todo objeto posible; y como
C2 es el unico criterio del veredicto (config.solo_c2), ninguno seria
convectivo y el tracker recibiria una lista vacia. Saltear no pierde nada.

El reciproco no vale: el gate puede dar True por un pixel alto que despues no
caiga dentro de ningun poligono detectado. Es un pre-filtro conservador — corre
TATHU de mas, nunca de menos.

Dos cosas sostienen esa garantia y no hay que romperlas:

  * El gate lee `cfg.echotop_dbz` y `cfg.echotop_min_above_h0_m`, los mismos
    parametros que usa C2. Hardcodear un umbral rompe la equivalencia.
  * El gate recorta a `cfg.r_max_km` antes del maximo, porque los objetos fuera
    de ese radio los descarta `in_domain`. Sin el recorte, un eco alto a 180 km
    enciende el gate para nada.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

# filtro_convectivo vive en el directorio padre. El bootstrap va aca y no solo
# en config.py para que gate.py se pueda importar suelto — es lo que hace
# test_gate.py, y un test que necesita arrancar medio programa para correr
# termina no corriendose.
_RAIZ_PROYECTO = Path(__file__).resolve().parent.parent
if str(_RAIZ_PROYECTO) not in sys.path:
    sys.path.insert(0, str(_RAIZ_PROYECTO))

import filtro_convectivo as fc  # noqa: E402

# Altitud de la antena de RMA1 sobre el nivel del mar.
RMA1_ALTITUD_M = 484.0


# --------------------------------------------------------------------------
# H0: WRF con respaldo climatologico. Nunca None.
# --------------------------------------------------------------------------

# Ciclo anual sinusoidal para la llanura subtropical argentina (~31 S). Ya no es
# la fuente principal: es el respaldo que hace que `resolver_h0` sea total.
H0_MEDIA_MSNM = 3650.0
H0_AMPLITUD_M = 950.0
H0_DOY_MAXIMO = 15


def h0_climatologico(fecha: datetime) -> float:
    """Isoterma de 0 C en metros s.n.m. para la fecha dada (ciclo anual)."""
    doy = fecha.timetuple().tm_yday
    fase = 2.0 * np.pi * (doy - H0_DOY_MAXIMO) / 365.25
    return float(H0_MEDIA_MSNM + H0_AMPLITUD_M * np.cos(fase))


def resolver_h0(timestamp: datetime, serie=None) -> tuple[float, str]:
    """H0 [m s.n.m.] para la hora del volumen. SIEMPRE devuelve un numero.

    Esta funcion es la que elimina el modo degradado. Antes, sin H0 el pipeline
    pasaba None, C2 quedaba "no evaluado" y — con C2 como unico criterio — todo
    objeto pasaba: el filtro se apagaba en silencio. Ahora la ausencia de WRF
    degrada a climatologia, que es peor que el modelo pero incomparablemente
    mejor que no filtrar.

    Devuelve (h0_msnm, fuente). La fuente se guarda en los attrs de cada
    sistema: con C2 como filtro unico, poder auditar despues que volumenes se
    filtraron con modelo y cuales con el ciclo anual deja de ser un lujo.
    """
    if serie is not None:
        try:
            valor = serie.en(timestamp)
            if np.isfinite(valor):
                return float(valor), "wrf"
        except Exception:
            # Corrida que no cubre la hora, serie vacia, lo que sea. Cae al
            # respaldo: el radar no se detiene porque el WRF no llego.
            pass
    return h0_climatologico(timestamp), "climatologia"


def h0_para_grilla(h0_msnm: float,
                   referencia: str,
                   altitud_radar_m: float = RMA1_ALTITUD_M) -> float:
    """Pasa H0 de m s.n.m. a la referencia vertical de la grilla.

    `referencia` no tiene default a proposito: es el error silencioso mas caro
    de todo el esquema y quien arma la grilla tiene que declararlo.

      * "radar" -> z_coords mide desde la antena (lo que entrega Py-ART)
      * "msnm"  -> z_coords mide desde el nivel del mar (habitual en productos
                   grillados ya interpolados)
    """
    if referencia == "radar":
        return h0_msnm - altitud_radar_m
    if referencia == "msnm":
        return h0_msnm
    raise ValueError(f"referencia={referencia!r} invalida; usa 'radar' o 'msnm'")


# --------------------------------------------------------------------------
# El gate
# --------------------------------------------------------------------------

@dataclass
class ResultadoGate:
    """Veredicto del gate y los numeros que lo justifican.

    Los diagnosticos no son decorativos: con el gate descartando volumenes
    enteros, `tope_max_m` contra `umbral_m` es lo unico que despues permite
    entender por que una tormenta no aparecio en la base.
    """
    pasa: bool
    tope_max_m: float          # maximo del tope de eco dentro de r_max, referencia de la grilla
    umbral_m: float            # h0 + echotop_min_above_h0_m, misma referencia
    h0_msnm: float
    h0_fuente: str
    n_pixeles_pasan: int
    referencia_z: str

    def __str__(self) -> str:
        estado = "PASA" if self.pasa else "descarta"
        tope = f"{self.tope_max_m:.0f}" if np.isfinite(self.tope_max_m) else "sin eco"
        return (f"{estado}  tope_max={tope} m  umbral={self.umbral_m:.0f} m  "
                f"px={self.n_pixeles_pasan}  H0={self.h0_msnm:.0f} m s.n.m. [{self.h0_fuente}]")


def evaluar(vol: fc.VolumenFiltrado,
            h0_msnm: float,
            h0_fuente: str,
            referencia_z: str,
            altitud_radar_m: float = RMA1_ALTITUD_M) -> ResultadoGate:
    """C2 aplicado al volumen entero. True = hay que correr TATHU.

    Trabaja sobre `vol.echo_top`, que `preparar_volumen()` calculo con
    `cfg.echotop_dbz` sobre el campo ya depurado por el QC polarimetrico. Usar
    el campo con QC importa: sobre DBZH crudo, ecos espurios en altura (segundo
    trayecto, RFI, interferencia solar) encienden el gate justo en los volumenes
    limpios que se querian saltear.
    """
    cfg = vol.cfg
    umbral = (h0_para_grilla(h0_msnm, referencia_z, altitud_radar_m)
              + cfg.echotop_min_above_h0_m)

    # Recorte al dominio util: fuera de r_max_km los objetos los descarta
    # in_domain, asi que un eco alto ahi no debe encender el gate.
    dentro = vol.dist_km <= cfg.r_max_km
    topes = np.where(dentro, vol.echo_top, np.nan)

    if not np.isfinite(topes).any():
        return ResultadoGate(False, float("nan"), umbral, h0_msnm,
                             h0_fuente, 0, referencia_z)

    tope_max = float(np.nanmax(topes))
    n_pasan = int(np.count_nonzero(topes >= umbral))
    return ResultadoGate(bool(tope_max >= umbral), tope_max, umbral,
                         h0_msnm, h0_fuente, n_pasan, referencia_z)


def serie_h0_actual(cfg, lat: float, lon: float):
    """SerieH0 de la ultima corrida del WRF, o None si no hay ninguna usable.

    Se llama una vez por corrida del WRF (no una por volumen): h0_wrf cachea el
    resultado por (corrida, punto), asi que el wrfout se lee una vez cada 12 h.
    Devolver None no es un error — `resolver_h0` cae a la climatologia.
    """
    try:
        import h0_wrf
        return h0_wrf.serie_h0(
            lat=lat, lon=lon,
            raiz=cfg.wrfout_raiz,
            params=cfg.wrf_params,
            dominio=cfg.wrf_dominio,
            edad_minima=cfg.wrf_edad_minima,
        )
    except Exception:
        return None
