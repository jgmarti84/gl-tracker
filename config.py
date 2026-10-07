# -*- coding: utf-8 -*-
"""Parametros del tracking operativo, todos en un solo lugar.

Nada de este paquete lee constantes sueltas: todo pasa por `Config`. La razon
es operativa — cuando el filtro descarta un volumen entero hay que poder
reconstruir despues con que numeros lo descarto, y eso solo funciona si los
numeros viven en un unico objeto que se puede loguear y serializar.

Las rutas se pueden pisar por variable de entorno para no tener que editar el
archivo en el servidor:

    RADAR_RAIZ    directorio de volumenes de RMA1
    WRFOUT_RAIZ   raiz de los wrfout del operativo
    TRACKING_OUT  directorio de salida (base de datos y estado)
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# filtro_convectivo vive en el directorio padre, junto al notebook. No se
# duplica aca a proposito: es la referencia del filtro y tiene que haber una
# sola copia.
_RAIZ_PROYECTO = Path(__file__).resolve().parent.parent
if str(_RAIZ_PROYECTO) not in sys.path:
    sys.path.insert(0, str(_RAIZ_PROYECTO))

import filtro_convectivo as fc  # noqa: E402


@dataclass
class Config:
    # ── Entrada: grilla de radar ya interpolada ──────────────────────────────
    # El grillado lo hace otro paso. Este programa consume su salida.
    grilla_raiz: Path = Path(os.getenv("GRILLA_RAIZ", "/home/datos/radar/grillas"))
    grilla_glob: str = "*.nc"
    # Segundos que un archivo debe llevar sin modificarse para considerarlo
    # cerrado. Leer una grilla a medio escribir da un tope de eco truncado, y
    # con C2 como filtro unico eso se traduce en descartar el volumen entero.
    grilla_edad_minima: float = 30.0

    # Nombres de las variables 3D dentro del netCDF. El lector prueba estos
    # primero y despues algunos alias habituales.
    var_dbzh: str = "DBZH"
    var_rhohv: str = "RHOHV"
    # True si la grilla viene con fila 0 = sur (convenio Py-ART). El resto del
    # programa asume fila 0 = norte (convenio GDAL) para que el raster y los
    # arrays 3D coincidan pixel a pixel.
    invertir_y: bool = False

    # ── Entrada: WRF ─────────────────────────────────────────────────────────
    wrfout_raiz: Path = Path(os.getenv("WRFOUT_RAIZ", "/home/datos/wrfdatos/wrfout"))
    # Parametrizaciones en ORDEN DE PREFERENCIA: se usa la primera disponible.
    # Mismo criterio que el script de meteogramas del OHMC, que recorre la lista
    # y corta en la primera que encuentra. Sin este orden, elegir por nombre se
    # quedaria con la D en vez de la A.
    wrf_params: tuple = ("A", "B", "C", "D")
    wrf_dominio: int = 1
    wrf_edad_minima: float = 300.0

    # Punto donde se toma la columna del WRF para calcular H0.
    #
    # Cordoba capital (Plaza San Martin). NO es el radar, y no tiene por que
    # serlo: H0 es un campo de gran escala que varia muy poco en esta escala
    # horizontal, y el punto solo elige que columna del modelo se lee.
    #
    # Ojo con confundirlo con radar_lat/radar_lon de mas abajo: ese par
    # georreferencia el raster y tiene que seguir siendo la antena.
    h0_lat: float = -31.4167
    h0_lon: float = -64.1833

    # ── Geometria del radar ──────────────────────────────────────────────────
    # Origen de la grilla, para georreferenciar el raster que consume TATHU.
    # Se usa SOLO si el netCDF de entrada no trae los atributos radar_lat /
    # radar_lon. Tiene que ser la antena: es donde x = y = 0.
    radar_lat: float = -31.44133      # RMA1
    radar_lon: float = -64.19199
    # Altitud de la antena. Solo se usa si la grilla no la trae como atributo, y
    # solo importa cuando referencia_z == 'radar'.
    radar_alt_m: float = 484.0

    # ── Salida ───────────────────────────────────────────────────────────────
    out_dir: Path = Path(os.getenv(
        "TRACKING_OUT", str(Path(__file__).resolve().parent / "out")))

    # ── Raster ───────────────────────────────────────────────────────────────
    fill_value: float = -30.0        # dBZ del no-data del raster

    # Referencia vertical de z. Se usa SOLO si el archivo de grilla no trae el
    # atributo global `referencia_z`; cuando lo trae, manda el archivo.
    #
    # Este es el parametro mas peligroso del programa. Py-ART entrega `grid.z`
    # en metros sobre el radar, pero un producto grillado generico suele venir
    # en msnm. Con RMA1 a 484 m, equivocarse son 484 m de error sobre un margen
    # C2 de 1500 m — y en el barrido de malla_c1_c2.csv eso mueve la retencion
    # de 263 a ~304 objetos (+16%), sin ningun sintoma visible.
    referencia_z: str = "radar"

    # ── Deteccion ────────────────────────────────────────────────────────────
    dbz_threshold: float = 35.0
    min_area_km2: float = 50.0

    # ── Filtro convectivo ────────────────────────────────────────────────────
    # Campos de fc.ConvectiveFilterConfig a pisar, p.ej. {'r_max_km': 120.0}.
    filter_overrides: dict = field(default_factory=dict)

    # Solo C2 decide. C1 y C3 se siguen calculando y guardando como diagnostico
    # (cuestan milisegundos y permiten re-evaluar umbrales sobre corridas viejas
    # sin reprocesar volumenes), pero no entran en el veredicto.
    #
    # Justificacion empirica, del barrido de out/evaluacion/*/malla_c1_c2.csv:
    # con C2=1500 fijo, mover C1 de 0 a 20 km2 da resultados identicos hasta el
    # ultimo decimal (263 retenidos, 36.9% descartado, mismo IoU). Y sobre las 5
    # situaciones evaluadas, de 665 objetos que pasan C2 solo 2 fallan C1 y
    # ninguno falla C3. C1 ya lo impone la deteccion (min_area_km2=50) y C3 ya
    # lo hizo el QC polarimetrico de la etapa A.
    solo_c2: bool = True

    # ── Tracking ─────────────────────────────────────────────────────────────
    strategy: str = "relative"       # relative | absolute | titan | intersects
    overlap: float = 0.10
    titan_threshold: float = 0.50

    # Si entre el volumen anterior procesado y el actual pasa mas que esto, se
    # descarta `previous` y las trayectorias arrancan de cero. Sin esto, tras
    # una racha de volumenes descartados por el gate el tracker asociaria
    # sistemas separados por media hora, inventando trayectorias.
    # RMA1 cicla cada ~9 min: 20 min son dos ciclos perdidos.
    max_gap_tracking_min: float = 20.0

    # ── Forecast ─────────────────────────────────────────────────────────────
    forecast_intervals: tuple = (9, 18, 27, 36, 45, 54, 63)   # minutos
    apply_scale: bool = True
    use_history_velocity: bool = True
    velocity_history_len: int = 4

    # ── Bucle operativo ──────────────────────────────────────────────────────
    intervalo_sondeo_s: float = 60.0   # cada cuanto mirar si llego un volumen nuevo

    # ── Derivados ────────────────────────────────────────────────────────────

    @property
    def db_path(self) -> Path:
        return self.out_dir / "systems-db.sqlite"

    @property
    def estado_path(self) -> Path:
        return self.out_dir / "estado_tracking.pkl"

    def filtro(self) -> fc.ConvectiveFilterConfig:
        """ConvectiveFilterConfig con los overrides aplicados.

        `det_threshold_dbz` y `det_min_area_km2` del filtro se sincronizan con
        los de deteccion para que toda la parametrizacion sea coherente aunque
        se lean desde dos lados.
        """
        cfg = fc.ConvectiveFilterConfig(**self.filter_overrides)
        cfg.det_threshold_dbz = self.dbz_threshold
        cfg.det_min_area_km2 = self.min_area_km2
        return cfg


# Atributos que se persisten como columnas de la tabla. Identicos a los del
# notebook para que las bases sean intercambiables.
STAT_ATTRS = ["min", "max", "mean", "std", "median", "count"]
MOVEMENT_ATTRS = ["nae", "velocity", "u", "v", "direction"]
# Solo numericos: spatialite.Outputter crea las columnas dinamicas como REAL,
# asi que 'mode' (string) queda en attrs pero no se persiste. Los bool entran
# como 0/1 y los None como NULL.
CONV_ATTRS = ["is_convective", "c1_core", "c2_vertical", "c3_polarimetric",
              "in_domain", "core_area_km2", "core_max_dbz", "core_rhohv_mean",
              "echo_top_max_m", "dist_centroid_km", "h0_msnm"]
DB_ATTRS = STAT_ATTRS + MOVEMENT_ATTRS + CONV_ATTRS
