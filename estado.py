# -*- coding: utf-8 -*-
"""Estado del tracking entre volumenes.

El notebook procesa una lista de archivos de corrido y guarda `previous` en una
variable local. En operativo cada volumen llega por separado y el proceso se
puede reiniciar, asi que ese estado tiene que sobrevivir a disco.

Que se guarda y por que:

  * `ultimo_timestamp` — hasta donde se proceso. Evita reprocesar y permite
    detectar el hueco temporal que invalida el tracking.
  * `previous` — los sistemas del ultimo volumen procesado, para asociar contra
    ellos. Se serializan a WKT: `ConvectiveSystem.geom` es una geometria OGR y
    OGR no es picklable.
  * `historia` — las ultimas posiciones de cada sistema, que alimentan la
    velocidad ponderada del forecast. Se guarda como dict de listas y no como
    `defaultdict(lambda: deque(...))` porque un lambda no se puede picklear.

El `name` (uuid) de cada sistema se preserva. Es lo que mantiene la identidad de
una trayectoria a traves de un reinicio: sin eso, cada arranque cortaria todas
las familias vivas y las volveria a numerar.
"""

from __future__ import annotations

import pickle
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from osgeo import ogr

from tathu.tracking.system import ConvectiveSystem


@dataclass
class Estado:
    ultimo_timestamp: Optional[datetime] = None
    previous: list = field(default_factory=list)          # serializado (dicts)
    historia: dict = field(default_factory=dict)          # {name: [(ts, cx, cy), ...]}


# --------------------------------------------------------------------------
# Serializacion de sistemas
# --------------------------------------------------------------------------

def serializar_sistemas(systems) -> list[dict]:
    """ConvectiveSystem -> dicts pickleables (geometria a WKT)."""
    return [{
        "name": s.name,
        "wkt": s.geom.ExportToWkt(),
        "timestamp": s.timestamp,
        "attrs": dict(s.attrs),
        "event": s.event,
    } for s in systems]


def deserializar_sistemas(crudos: list[dict]) -> list:
    """dicts -> ConvectiveSystem utilizables como `previous` del tracker.

    Las `relationships` no se restauran: el tracker solo las escribe sobre los
    sistemas del volumen ACTUAL, y el forecaster las lee de ahi. Lo que si
    importa preservar es el timestamp, porque de el sale el dt del pronostico.
    """
    systems = []
    for c in crudos:
        s = ConvectiveSystem(ogr.CreateGeometryFromWkt(c["wkt"]))
        s.name = c["name"]
        s.timestamp = c["timestamp"]
        s.attrs = dict(c["attrs"])
        s.event = c["event"]
        systems.append(s)
    return systems


# --------------------------------------------------------------------------
# Historia de velocidades
# --------------------------------------------------------------------------

def historia_a_deques(historia: dict, maxlen: int):
    """dict de listas -> defaultdict de deques, que es lo que usa el forecast."""
    d = defaultdict(lambda: deque(maxlen=maxlen))
    for nombre, puntos in historia.items():
        d[nombre] = deque(puntos, maxlen=maxlen)
    return d


def deques_a_historia(deques) -> dict:
    """defaultdict de deques -> dict de listas, pickleable."""
    return {nombre: list(puntos) for nombre, puntos in deques.items() if puntos}


def podar_historia(historia: dict, vivos: set) -> dict:
    """Descarta la historia de sistemas que ya no existen.

    Sin esto el pickle crece sin limite: cada tormenta que pasa deja su rastro
    para siempre y el estado termina pesando mas que la base de datos.
    """
    return {n: p for n, p in historia.items() if n in vivos}


# --------------------------------------------------------------------------
# Persistencia
# --------------------------------------------------------------------------

def cargar(ruta: Path) -> Estado:
    """Estado guardado, o uno vacio si no hay o si esta corrupto.

    Un estado ilegible no aborta el arranque: se pierde la continuidad de las
    trayectorias vivas, que es molesto, pero el operativo sigue corriendo.
    """
    if not ruta.is_file():
        return Estado()
    try:
        with open(ruta, "rb") as f:
            e = pickle.load(f)
        if isinstance(e, Estado):
            return e
    except Exception:
        pass
    return Estado()


def guardar(ruta: Path, estado: Estado) -> None:
    """Escritura atomica: se escribe a un temporal y se renombra.

    Si el proceso muere a mitad de un pickle.dump, el archivo queda truncado y
    al arrancar se pierde el estado. El rename es atomico en el mismo
    filesystem, asi que o esta el estado viejo completo o el nuevo completo.
    """
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(ruta.suffix + ".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(estado, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(ruta)
