# mercosur-road-data

Paquetes de datos viales por región del **Mercosur** (provincias de
Argentina, estados de Brasil, Paraguay, Uruguay y Bolivia), para apps de
navegación: **velocidades máximas** (con el trazado de cada tramo),
**semáforos** y **radares** de velocidad, con el sentido que controlan
cuando está cargado.

Se generan **los lunes y jueves** de madrugada a partir de los extractos diarios de
OpenStreetMap ([openstreetmap.fr](https://download.openstreetmap.fr/extracts/south-america/),
[Geofabrik](https://download.geofabrik.de/south-america/)) y se publican
en la release [`data`](../../releases/tag/data):

- `index.json`: catálogo (id, nombre, país, fecha, zona, tamaño, cantidades, URL).
- `<id>.json.gz`: un paquete por región (por ejemplo `ar-misiones`, `br-parana`, `py`).

## Formato de un paquete

```json
{
  "id": "ar-misiones", "name": "Misiones", "country": "AR", "generated": "2026-11-17",
  "bbox": [minLon, minLat, maxLon, maxLat],
  "speed":   [[kmh, "tipo de vía", "nombre", [lon, lat, lon, lat, ...]], ...],
  "signals": [[lat, lon, sentido], ...],
  "radars":  [[lat, lon, kmh, sentido], ...]
}
```

`sentido`: grados (0 = norte) del tránsito que controla; `-1` si no se sabe
o controla las dos manos.

`signals` incluye los semáforos de esquina (`highway=traffic_signals`) y
los **cruces peatonales con semáforo** (`crossing=traffic_signals`) que
están sobre calles de autos, uno por esquina: un cruce a menos de 35 m de
otro semáforo ya contado no se agrega.

`tolls`: `[[lat, lon, "nombre", precio, "moneda"], ...]` cabinas de peaje
(`barrier=toll_booth`), una por plaza (300 m); `precio` 0 si no está
cargado (`charge`).

`police`: `[[lat, lon, "código", "nombre completo"], ...]` puestos de
control policial (`barrier=checkpoint`, `police=checkpoint` o
`amenity=police` con nombre de puesto/control/caminera…), con la fuerza
según `operator`/`name`: GN, PNA, PFA, POL (provincial), PRF, BM, PM, PC,
PN o CTRL (sin identificar).

## Generar a mano

```
pip install osmium
python3 scripts/build_all.py ar-misiones py   # sin argumentos: todas
```

Las regiones están en `regions.json`. La tarea `.github/workflows/build.yml`
corre los lunes y jueves y también se puede lanzar a mano (Actions → "Generar
paquetes de regiones" → Run workflow).

## Licencia

Datos © colaboradores de OpenStreetMap, disponibles bajo la
[Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/).
Los paquetes son una base de datos derivada y se comparten bajo la misma
licencia. Los datos pueden tener errores u omisiones: la velocidad que manda
es la de la señal en la calle.

## Red de calles (rutas sin conexión, etapa 1)

Cada región trae además `<id>.graph.gz` (`scripts/build_graph.py`): las
calles por donde va un auto, con largo, velocidad, sentido, tipo, tierra,
peaje, rotonda, nombre, dibujo y restricciones de giro, en un formato binario
compacto ("AHG1", detallado en el script). En el catálogo: `graphUrl` y
`graphBytes`. Prueba con Misiones: 144.565 esquinas, 191.436 tramos
(58.234 km), 5,4 MB; San Vicente → Posadas da 198 km por RP13, RN14, RN12 y
RN105, igual que Valhalla.
