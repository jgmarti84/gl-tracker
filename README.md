# Tracking operativo con gate C2

Seguimiento de sistemas convectivos sobre RMA1 (SINARAME). Antes de detectar
nada, descarta el volumen entero si no hay desarrollo vertical por encima de la
isoterma de 0 °C pronosticada por el WRF.

La entrada es la **grilla de radar ya interpolada**: el grillado lo hace otro
paso, aguas arriba de este programa.

```
entra la grilla interpolada (nz, ny, nx)
          │
H0 de la última corrida del WRF en Córdoba capital
          │
C2 sobre TODO el volumen: ¿algún tope de eco sobre H0 + margen?
          │
    sí ──▶ detección ─▶ clasificación ─▶ tracking ─▶ forecast ─▶ SQLite
    no ──▶ se descarta el volumen y se espera la siguiente imagen
```

Un volumen tarda **~0,3 s** punta a punta.

---

## Requisitos

Python 3.10 o superior (usa `X | None` en anotaciones). Probado en 3.12.3.

| paquete | probado con | para qué |
|---|---|---|
| `numpy` | 2.4.6 | todo |
| `scipy` | 1.17.1 | `ndimage` en el QC del filtro |
| `netCDF4` | 1.7.4 | leer la grilla y los wrfout |
| `gdal` (`osgeo`) | 3.8.4 | raster en memoria para el detector |
| `rasterio` | 1.5.1 | rasterizar polígonos de sistemas |
| `shapely` | 2.1.2 | ídem |
| `affine` | 3.0.0 | geotransform |
| `tathu` | 0.1.0 | detección, tracking, forecast, salida |

Además la extensión de SQLite **`mod_spatialite`**, que `tathu.io.spatialite`
carga con `load_extension`. Sin ella no se crea la base.

> **Py-ART no hace falta.** Lo usaba el grillado, que ahora está afuera. Si el
> entorno lo tiene por otras razones no molesta, pero no es dependencia.

### Disposición en disco

`filtro_convectivo.py` **no** se duplica acá: vive en el directorio padre y es
la referencia única del filtro. `config.py` y `gate.py` lo agregan a `sys.path`
por ruta relativa, así que la estructura tiene que ser esta:

```
<raíz>/
├── filtro_convectivo.py        ← requerido, no se toca
└── tracking_operativo/
    ├── operativo.py
    └── ...
```

---

## Configuración

Todo vive en `Config` (`config.py`). Las rutas se pisan por variable de entorno
o por flag; el resto se edita en el archivo.

| variable de entorno | flag | qué es |
|---|---|---|
| `GRILLA_RAIZ` | `--grilla-raiz` | directorio de grillas interpoladas |
| `WRFOUT_RAIZ` | `--wrfout-raiz` | raíz de los wrfout del operativo |
| `TRACKING_OUT` | `--out` | salida: base de datos y estado |

Lo que hay que revisar antes de la primera corrida:

| parámetro | default | nota |
|---|---|---|
| `referencia_z` | `"radar"` | **el más peligroso.** Ver *Contrato de entrada*. |
| `var_dbzh` / `var_rhohv` | `DBZH` / `RHOHV` | nombres en el netCDF de entrada |
| `invertir_y` | `False` | `True` si la grilla viene con fila 0 = sur |
| `wrf_params` | `("A","B","C","D")` | parametrizaciones en **orden de preferencia**; gana la primera disponible |
| `h0_lat` / `h0_lon` | Córdoba capital | punto donde se toma la columna del WRF para H0 |
| `radar_lat` / `radar_lon` | RMA1 | origen del raster; **tiene que ser la antena** |
| `grilla_glob` | `*.nc` | acotarlo si el directorio tiene otras cosas |
| `grilla_edad_minima` | 30 s | segundos sin modificarse para dar por cerrado un archivo |
| `max_gap_tracking_min` | 20 min | hueco máximo que tolera el tracking |

---

## Contrato de entrada

Un netCDF por volumen, con:

- Dos variables 3D `(nz, ny, nx)` float32, **NaN = sin dato**: reflectividad y
  RHOHV. El convenio NaN es obligatorio — rellenar con un valor de fondo
  desactiva el despeckle del QC.
- Coordenadas 1D: `x_km`, `y_km` (km desde el radar) y `z_m`. Si vienen en
  metros con nombres `x`/`y`, el lector las convierte solo.
- **Fila 0 = norte** (convenio GDAL). Si la grilla viene con fila 0 = sur
  (convenio Py-ART), poner `Config.invertir_y = True`.

Atributos globales, opcionales pero recomendados: `referencia_z`, `timestamp`,
`radar_lat`, `radar_lon`, `radar_alt_m`. Si no están se usan los de `Config` y
el nombre del archivo.

El timestamp sale del nombre del archivo por regex `\d{8}T\d{6}` — por ejemplo
`grid_RMA1_0315_01_20260101T173058Z.nc`. Un archivo sin ese patrón se ignora.

RHOHV es obligatorio. Si llega vacío, `grilla.leer()` levanta excepción y se
saltea ese volumen: sin RHOHV no corre el QC ni la máscara de capa de fusión, y
el tope de eco calculado sobre reflectividad cruda no es confiable. Afecta a
~3 % de los volúmenes de RMA1.

### Dónde se calcula H0

`h0_lat` / `h0_lon` apuntan a **Córdoba capital** (Plaza San Martín,
−31.4167, −64.1833), no al radar. H0 es un campo de gran escala y varía muy
poco en esta escala horizontal: entre ese punto y RMA1 hay 2,86 km, y sobre
una corrida de 82 horas la diferencia de H0 fue de +4 m en media, 24 m rms y
129 m en el peor tiempo — un 8,6 % del margen C2 de 1500 m.

> **No confundir con `radar_lat` / `radar_lon`.** Ese par georreferencia el
> raster que consume TATHU y tiene que seguir siendo la antena: es donde
> x = y = 0. Moverlo correría todos los sistemas detectados en el mapa.

### Selección de la corrida del WRF

`descubrir_ultima_corrida()` escanea `WRFOUT_RAIZ` y aplica dos niveles de
respaldo, en este orden:

1. Dentro de una misma corrida, prueba las parametrizaciones en el orden de
   `wrf_params` y toma la primera disponible. Es el mismo criterio del script
   de meteogramas del OHMC, que recorre la lista y corta con `break` en la
   primera que encuentra. **El orden importa**: elegir por nombre se quedaría
   con la D en vez de la A.
2. Si ninguna parametrizacion de la corrida más nueva está completa —porque el
   WRF todavía la está escribiendo—, pasa a la corrida anterior. Un H0 de hace
   12 h es mucho mejor que ninguno.

A diferencia del script de meteogramas, la corrida **no** se deduce del reloj
(18 UTC del día anterior si es antes del mediodía, 06 UTC si no). Se descubre
del filesystem, así que si una corrida falló o llegó tarde el programa usa la
que realmente existe en vez de pedir un archivo que no está.

### La referencia vertical

Es el único dato que no se puede adivinar y que **falla en silencio**. Si el
archivo trae el atributo `referencia_z` (`"radar"` o `"msnm"`), manda el
archivo. Si no lo trae, se usa `Config.referencia_z` y el lector emite un
`RuntimeWarning`.

Con RMA1 a 484 m, equivocarse son 484 m de error sobre un margen C2 de 1500 m.
En el barrido de `../out/evaluacion/*/malla_c1_c2.csv` eso mueve la retención de
263 a ~304 objetos (+16 %), sin ningún síntoma visible.

**Si el paso de grillado puede escribir ese atributo, es la forma más barata de
cerrar el agujero para siempre.**

---

## Uso

```bash
python operativo.py                  # demonio: sondea y espera grillas nuevas
python operativo.py --una-vez        # procesa lo pendiente y sale (para cron)
python operativo.py --reprocesar 20  # las últimas 20 grillas, estado nuevo
python operativo.py --solo-gate      # evalúa el gate sin correr TATHU
python operativo.py --desde-cero     # ignora el estado guardado
python operativo.py --max-backlog 5  # acota la recuperación tras una caída

python test_gate.py                  # verifica la garantía del gate
python h0_wrf.py --lat -31.4167 --lon -64.1833     # inspeccionar H0
```

Sale con 0 si terminó bien, 1 si no encontró grillas con `--reprocesar` o si
`h0_wrf.py` no pudo resolver una corrida. Loguea a stdout con timestamp UTC.

### Como servicio

Se detiene limpio con Ctrl-C o SIGTERM: termina el volumen en curso, guarda el
estado y sale. El estado se escribe de forma atómica y la base commitea por
volumen, así que matarlo a mitad no corrompe nada — solo se pierde ese volumen,
que se reprocesa al arrancar de nuevo. Es seguro ponerlo bajo systemd con
`Restart=always`.

Un volumen corrupto no tumba el proceso: se loguea el error y se sigue, sin
tocar el estado, de modo que el siguiente volumen bueno asocia contra el último
frame válido.

---

## Salida

SQLite + SpatiaLite en `<out>/systems-db.sqlite`:

- Tabla `systems` — un registro por sistema convectivo por volumen.
- Tablas `forecast_9min` … `forecast_63min` — siete horizontes de pronóstico.

Columnas dinámicas (todas REAL): estadísticas zonales (`min`, `max`, `mean`,
`std`, `median`, `count`), movimiento (`nae`, `velocity`, `u`, `v`,
`direction`) y diagnóstico del filtro (`is_convective`, `c1_core`,
`c2_vertical`, `c3_polarimetric`, `in_domain`, `core_area_km2`, `core_max_dbz`,
`core_rhohv_mean`, `echo_top_max_m`, `dist_centroid_km`, `h0_msnm`).

El estado del tracking va aparte, en `<out>/estado_tracking.pkl`.

---

## Decisiones de diseño que conviene no romper

### 1. Por qué el gate no pierde nada

C2 por objeto es `max(echo_top[mask_obj]) >= h0 + margen`. El máximo sobre todos
los píxeles es siempre ≥ el máximo sobre los píxeles de cualquier objeto. Si el
gate da `False`, C2 falla para todo objeto posible; y como C2 es el único
criterio del veredicto, ninguno sería convectivo y el tracker recibiría una
lista vacía. **Saltear no pierde nada.**

El recíproco no vale: el gate puede dar `True` por un píxel alto que después no
caiga dentro de ningún polígono detectado. Es un pre-filtro conservador — corre
TATHU de más, nunca de menos.

Dos cosas sostienen esa garantía, y `test_gate.py` las verifica:

- El gate lee `cfg.echotop_dbz` y `cfg.echotop_min_above_h0_m`, **nunca**
  constantes propias. Hardcodear un umbral hace que al mover C2 el gate empiece
  a descartar volúmenes que sí tenían sistemas.
- El gate recorta a `cfg.r_max_km` **antes** de tomar el máximo, porque los
  objetos fuera de ese radio los descarta `in_domain` igual.

### 2. Por qué solo C2

C1 y C3 se siguen calculando y guardando como diagnóstico, pero no entran en el
veredicto (`Config.solo_c2`). La razón es empírica, del barrido que ya estaba
corrido en `../out/evaluacion/`:

| C1 (km²) | retenidos | % descartado | cobertura | IoU |
|---:|---:|---:|---:|---:|
| 0 | 263 | 36,9 % | 29,6 % | 0,0500 |
| 4 | 263 | 36,9 % | 29,6 % | 0,0500 |
| 8 | 263 | 36,9 % | 29,6 % | 0,0500 |
| 20 | 263 | 36,9 % | 29,6 % | 0,0500 |
| 30 | 255 | 38,8 % | 29,3 % | 0,0576 |

Con C2 fijo en 1500 m, mover C1 de 0 a 20 km² da resultados **idénticos**.
Objeto por objeto sobre las cinco situaciones evaluadas (1066 objetos): de los
665 que pasan C2, solo **2 fallan C1** y **ninguno falla C3**.

No es casualidad. C1 ya lo impone la detección (`min_area_km2 = 50` sobre
≥35 dBZ: el percentil 1 del área de núcleo que sobrevive es 21 km², muy por
encima del umbral de 8). Y C3 ya lo hizo el QC polarimétrico de la etapa A, que
filtra píxel a píxel con `rhohv_qc_min = 0.85`; C3 mide RHOHV dentro del núcleo
de ≥40 dBZ, donde es naturalmente alto.

> **Salvedad.** Son cinco situaciones de RMA1. Que C1 y C3 sean inertes es un
> hecho sobre *estos umbrales en estos casos*, no una ley. En invierno H0 baja a
> ~2700 m y el margen de 1500 m se vuelve más fácil de superar; ahí C2 puede
> volverse permisivo y la ausencia de C1 sí notarse.

### 3. No hay modo degradado

`gate.resolver_h0()` **siempre** devuelve un número. Si no hay corrida del WRF,
si no cubre la hora, si el disco no responde, cae a la climatología.

Esto no es un detalle. Antes, sin H0 el pipeline pasaba `None`, C2 quedaba «no
evaluado» y —con C2 como criterio único— todo objeto pasaba: el filtro se
apagaba en silencio. La fuente (`wrf` / `climatologia`) se guarda en los attrs
de cada sistema para poder auditarlo después.

### 4. El estado entre volúmenes

`OverlapAreaTracker` necesita el frame anterior. En operativo cada volumen llega
por separado y el proceso se puede reiniciar, así que `estado.py` lo persiste:
`previous` serializado a WKT (las geometrías OGR no son pickleables) y la
historia de velocidades como dict de listas (un `defaultdict(lambda: ...)`
tampoco se puede picklear). El `name` (uuid) se preserva, que es lo que mantiene
la identidad de una trayectoria a través de un reinicio.

Dos reglas:

- **Volumen descartado por el gate ⇒ `previous = []`.** Es exactamente
  equivalente a procesarlo y no obtener sistemas: por la garantía del gate,
  ninguno habría pasado. Dejar `previous` intacto haría que el próximo volumen
  asociara contra sistemas viejos.
- **Hueco > `max_gap_tracking_min` ⇒ se reinicia el tracking.** A 15 m/s un
  sistema recorre 18 km en 20 min y ya no solapa consigo mismo. RMA1 cicla cada
  ~9 min, así que 20 min tolera exactamente un volumen perdido.

---

## Módulos

| archivo | responsabilidad |
|---|---|
| `operativo.py` | punto de entrada: demonio o corrida única |
| `config.py` | todos los parámetros, en un solo objeto |
| `grilla.py` | descubrimiento y lectura de la grilla interpolada |
| `h0_wrf.py` | última corrida del WRF → H0 en un punto |
| `gate.py` | resolución de H0 y el gate C2 de volumen |
| `pipeline.py` | detección, clasificación, tracking, forecast y salida |
| `estado.py` | persistencia del estado del tracking entre volúmenes |
| `test_gate.py` | propiedades que el gate no puede romper |

`grilla.py` es el único módulo que conoce el formato de entrada. Si el paso de
grillado cambia de formato (GeoTIFF apilado, zarr, npz), el cambio queda
contenido en `grilla.leer()`.

---

## Pendientes

1. **Confirmar `referencia_z`** de la grilla que entregue el paso de grillado.
   Ver *Contrato de entrada*.
2. **Confirmar el origen de la grilla.** `preparar_volumen()` calcula
   `dist_km = sqrt(x² + y²)` asumiendo que el origen es la antena. Si la grilla
   está en una proyección regional con otro origen, `in_domain` y el recorte a
   `r_max_km` filtran por el lugar equivocado.
3. **Medir el descarte real** con `--solo-gate` sobre situaciones con relleno
   estratiforme. La simulación sobre el caché de evaluación dio una cota
   superior de 31 % de volúmenes descartados, con mucha varianza entre
   situaciones (0 % en un evento severo puro, 48 % en uno nocturno).

Con el grillado afuera el gate ya casi no ahorra CPU: son 0,3 s por volumen. Lo
que ahorra es aguas abajo — filas de base, polígonos de forecast, ruido para
quien consuma la salida — más una decisión explícita y logueada de «acá no hay
nada convectivo». Si el objetivo fuera rendimiento, el lugar para atacarlo es el
paso de grillado.

---

## Verificación

```
$ python test_gate.py
test_gate: OK
```

Siete propiedades, incluida la garantía del gate sobre 80 torres sintéticas
aleatorias: nunca ocurre que el gate dé `False` y algún objeto pase C2.

Se comprobó que el test efectivamente falla: sobre copias del paquete con el
recorte de `r_max_km` quitado, con el margen de C2 hardcodeado y con
`resolver_h0()` devolviendo `NaN`, las tres versiones dan `1 fallo(s)`.

Corrida punta a punta sobre 19 volúmenes de RMA1 del 1-ene-2026 (evento de
granizo y viento) con los wrfout del dominio d01 a 4 km:

```
17:30:58  detectados=  2  convectivos=  2  rastreados=  0  H0=4554[wrf]  [0.2s]
17:39:55  detectados=  3  convectivos=  2  rastreados=  2  H0=4570[wrf]  [0.2s]
17:48:53  detectados=  3  convectivos=  2  rastreados=  2  H0=4587[wrf]  [0.2s]
...
19:54:34  detectados=  4  convectivos=  3  rastreados=  2  H0=4497[wrf]  [0.2s]
20:12:29  detectados=  5  convectivos=  5  rastreados=  2  H0=4527[wrf]  [0.3s]
20:21:26  detectados=  7  convectivos=  7  rastreados=  5  H0=4552[wrf]  [0.3s]
```

El hueco entre 19:54 y 20:12 es el volumen sin RHOHV: se saltea y aun así el
siguiente rastrea, porque los 17,9 min de hueco quedan bajo
`max_gap_tracking_min`.
