"""Velocidades de fuentes oficiales para completar o corregir OpenStreetMap.

- Brasil: placas de velocidad maxima de las concesiones federales (ANTT) y
  limites oficiales de Rio de Janeiro y Sao Paulo, juntados por el proyecto
  abierto Pardal (github.com/Murkiriel/pardal, ODbL), por estado.
- Argentina: tipo de via de las rutas nacionales del IGN / Vialidad Nacional
  (Ruta, Autovia, Autopista) para poner la maxima de la Ley 24.449 donde
  OpenStreetMap no tiene velocidad cargada.

Todo se baja una vez por corrida y queda en work/ (si falla, el paquete sale
igual, solo con OpenStreetMap).
"""
import csv
import io
import json
import math
import os
import re
import urllib.request
import zipfile

PARDAL = "https://github.com/Murkiriel/pardal/releases/latest/download/pardal-{uf}.zip"
IGN = ("https://wms.ign.gob.ar/geoserver/transporte/ows?service=WFS&version=1.0.0&request=GetFeature"
       "&typeName=transporte%3Avial_nacional&outputFormat=application%2Fjson")

# Region de regions.json -> sigla del estado.
UF = {
    "br-acre": "AC", "br-alagoas": "AL", "br-amapa": "AP", "br-amazonas": "AM", "br-bahia": "BA",
    "br-ceara": "CE", "br-distrito-federal": "DF", "br-espirito-santo": "ES", "br-goias": "GO",
    "br-maranhao": "MA", "br-mato-grosso": "MT", "br-mato-grosso-do-sul": "MS", "br-minas-gerais": "MG",
    "br-para": "PA", "br-paraiba": "PB", "br-parana": "PR", "br-pernambuco": "PE", "br-piaui": "PI",
    "br-rio-de-janeiro": "RJ", "br-rio-grande-do-norte": "RN", "br-rio-grande-do-sul": "RS",
    "br-rondonia": "RO", "br-roraima": "RR", "br-santa-catarina": "SC", "br-sao-paulo": "SP",
    "br-sergipe": "SE", "br-tocantins": "TO",
}

# Fuentes de Pardal que son oficiales (las demas salen de OpenStreetMap).
OFFICIAL = {"ANTT", "RIO", "CET-SP"}

# Una placa baja (peaje, radar, obra) vale hasta la proxima placa; si la
# proxima esta lejos, Pardal la estira kilometros. Se corta a esta distancia.
LOW_LIMIT_KMH = 40
LOW_LIMIT_MAX_M = 1500

# Ley 24.449 art. 51 (autos): rutas 110, semiautopistas/autovias 120, autopistas 130.
AR_LEGAL = {"Ruta": 110, "Autovía": 120, "Autopista": 130}

UA = {"User-Agent": "mercosur-road-data"}


def _get(url, timeout=300):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def _cached(work, name, url):
    path = os.path.join(work, name)
    if not os.path.exists(path):
        data = _get(url)
        with open(path, "wb") as f:
            f.write(data)
    with open(path, "rb") as f:
        return f.read()


def dist_m(a, b):
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * 111320 * k, (b[1] - a[1]) * 110570)


def _cut(coords, max_m):
    out = [coords[0]]
    total = 0.0
    for p in coords[1:]:
        d = dist_m(out[-1], p)
        if total + d >= max_m:
            f = (max_m - total) / d if d else 0
            out.append((out[-1][0] + (p[0] - out[-1][0]) * f, out[-1][1] + (p[1] - out[-1][1]) * f))
            return out
        out.append(p)
        total += d
    return out


def brazil(region_id, work):
    """Tramos con limite oficial: [kmh, tipo, fuente, sentido (-1 = los dos), [(lon, lat), ...]]."""
    uf = UF.get(region_id)
    if not uf:
        return []
    raw = _cached(work, f"pardal-{uf}.zip", PARDAL.format(uf=uf))
    csv.field_size_limit(1 << 30)
    out = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = next((n for n in z.namelist() if n.endswith("limites_trechos.csv")), None)
        if not name:
            return []
        for r in csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8")):
            if r.get("source") not in OFFICIAL or r.get("estimated") == "1":
                continue
            try:
                kmh = int(float(r["limit_kmh"]))
                coords = [tuple(map(float, p.split())) for p in re.sub(r"^LINESTRING\(|\)$", "", r["wkt"].strip()).split(",")]
            except (ValueError, KeyError):
                continue
            if len(coords) < 2 or not 5 <= kmh <= 150:
                continue
            if kmh <= LOW_LIMIT_KMH:
                coords = _cut(coords, LOW_LIMIT_MAX_M)
            d = r.get("direction_deg", "").strip()
            out.append([kmh, r.get("highway") or "", r["source"], int(float(d)) if d else -1, coords])
    return out


class ArgentinaRoutes:
    """Rutas nacionales del IGN, para saber si un tramo es ruta, autovia o autopista."""

    CELL = 0.02

    def __init__(self, work):
        data = json.loads(_cached(work, "ign_vial_nacional.json", IGN))
        self.grid = {}
        self.segs = []
        for f in data.get("features", []):
            p = f.get("properties", {})
            kmh = AR_LEGAL.get(p.get("tipo_de_via_de_transporte"))
            number = (p.get("designacion_de_red_vial") or "").strip().upper()
            if not kmh or not number:
                continue
            g = f.get("geometry") or {}
            lines = g.get("coordinates", []) if g.get("type") == "MultiLineString" else [g.get("coordinates", [])]
            for line in lines:
                for a, b in zip(line, line[1:]):
                    i = len(self.segs)
                    self.segs.append((a[0], a[1], b[0], b[1], number, kmh))
                    for cx in range(int(math.floor(min(a[0], b[0]) / self.CELL)), int(math.floor(max(a[0], b[0]) / self.CELL)) + 1):
                        for cy in range(int(math.floor(min(a[1], b[1]) / self.CELL)), int(math.floor(max(a[1], b[1]) / self.CELL)) + 1):
                            self.grid.setdefault((cx, cy), []).append(i)

    def legal_at(self, lon, lat, number, radius_m=60):
        """Maxima legal del tramo de la ruta `number` mas cercano, o None."""
        k = math.cos(math.radians(lat)) * 111320
        best = None
        cx, cy = int(math.floor(lon / self.CELL)), int(math.floor(lat / self.CELL))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.grid.get((cx + dx, cy + dy), ()):
                    x1, y1, x2, y2, num, kmh = self.segs[i]
                    if num != number:
                        continue
                    ax, ay = (x1 - lon) * k, (y1 - lat) * 110570
                    bx, by = (x2 - lon) * k, (y2 - lat) * 110570
                    vx, vy = bx - ax, by - ay
                    ll = vx * vx + vy * vy
                    t = 0 if ll == 0 else max(0.0, min(1.0, -(ax * vx + ay * vy) / ll))
                    d = math.hypot(ax + t * vx, ay + t * vy)
                    if d <= radius_m and (best is None or d < best[0]):
                        best = (d, kmh)
        return best[1] if best else None


NATIONAL_REF = re.compile(r"\bRN\s*-?\s*(\d+[A-Z]?)\b", re.I)


def national_number(tags):
    """Numero de ruta nacional argentina ("14") de las etiquetas de OSM, o None."""
    for key in ("ref", "official_ref", "name", "official_name"):
        v = tags.get(key) or ""
        m = NATIONAL_REF.search(v)
        if m:
            return m.group(1).upper()
        m = re.search(r"Ruta Nacional\s+(?:N[°º.]?\s*)?(\d+[A-Z]?)\b", v, re.I)
        if m:
            return m.group(1).upper()
    return None


URBAN_NAME = re.compile(r"^(avenida|av\.?|calle|boulevard|bulevar|bv\.?|costanera|acceso|rotonda)\b", re.I)


def looks_urban(tags):
    """Rotondas, avenidas y calles (aunque lleven el numero de la ruta): no se completan con la de ruta."""
    return tags.get("junction") in ("roundabout", "circular") or bool(URBAN_NAME.match(tags.get("name") or ""))


# Pueblos: dentro de este radio la maxima es la urbana (no se completa con la de ruta).
URBAN_RADIUS_M = {"city": 4000, "town": 2000, "village": 900, "hamlet": 300, "suburb": 1200, "neighbourhood": 500}


class Towns:
    CELL = 0.05

    def __init__(self, places):
        self.grid = {}
        for lon, lat, kind in places:
            r = URBAN_RADIUS_M.get(kind)
            if r:
                self.grid.setdefault((int(math.floor(lon / self.CELL)), int(math.floor(lat / self.CELL))), []).append((lon, lat, r))

    def urban(self, lon, lat):
        cx, cy = int(math.floor(lon / self.CELL)), int(math.floor(lat / self.CELL))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for plon, plat, r in self.grid.get((cx + dx, cy + dy), ()):
                    if dist_m((lon, lat), (plon, plat)) < r:
                        return True
        return False


def argentina_legal_rows(candidates, routes, towns):
    """Tramos de rutas nacionales sin maxima en OSM -> filas con la maxima de la ley.

    candidates: [(numero, tipo OSM, nombre, [(lon, lat), ...])]. Se parte cada
    tramo en pedazos fuera de pueblos y con el mismo tipo de via del IGN.
    """
    rows = []
    for number, hw, name, coords in candidates:
        run, run_kmh = [], None

        def flush():
            if len(run) >= 2 and run_kmh:
                rows.append([run_kmh, hw, name, [c for p in run for c in p], "ley"])

        for lon, lat in coords:
            kmh = None if towns.urban(lon, lat) else routes.legal_at(lon, lat, number)
            if kmh != run_kmh:
                if run and kmh is not None and run_kmh is not None:
                    run.append((lon, lat))  # que los pedazos se toquen
                flush()
                run = [(lon, lat)] if kmh is not None else []
                run_kmh = kmh
            elif kmh is not None:
                run.append((lon, lat))
        flush()
    return rows
