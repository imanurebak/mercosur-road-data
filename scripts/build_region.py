"""Convierte un extracto .osm.pbf de OpenStreetMap en un paquete de region
para apps de navegacion: velocidades maximas (con trazado), semaforos y
radares, con el sentido que controlan cuando esta cargado.

Uso: python3 build_region.py extracto.osm.pbf salida.json ID NOMBRE PAIS FECHA
"""
import json
import math
import re
import sys

import os

import osmium

import official_speeds
import signal_dirs

ROADS = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
         "living_street", "service", "motorway_link", "trunk_link", "primary_link", "secondary_link",
         "tertiary_link"}
ZONES = {"urban": 40, "rural": 110, "motorway": 130, "living_street": 10, "walk": 10}


def parse_speed(value):
    v = (value or "").strip().lower()
    if not v:
        return None
    if v == "walk":
        return 10
    if ":" in v:  # "AR:urban", "BR:rural"...
        return ZONES.get(v.split(":", 1)[1])
    d = re.search(r"\d+", v)
    if not d:
        return None
    n = int(d.group())
    return int(n * 1.609) if "mph" in v else n


def bearing(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[1], a[0], b[1], b[0]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def direction_of(tags):
    return (tags.get("traffic_signals:direction") or tags.get("direction") or "").strip().lower()


# ---- Peajes y controles policiales (comun a build_osm.py y build_region.py) ----
import unicodedata as _ud


def _norm(s):
    s = _ud.normalize("NFD", s or "")
    return "".join(c for c in s if _ud.category(c) != "Mn").lower()


POLICE_WORDS = ("puesto", "control", "posto", "caminer", "barrera", "checkpoint", "fiscaliza")
# Sedes, bases y oficinas: no son controles en la ruta (aunque digan "control").
POLICE_NOT = ("escuadron", "agrupacion", "seccion", "destacamento", "comisaria", "jefatura", "cuartel",
              "batallon", "delegacion", "unidad regional", "superintendencia", "delegacia", "batalhao",
              "companhia", "sede", "dependencia", "departamento", "oficina", "division")


def police_force(tags, country, region_name):
    """(codigo, nombre completo) de la fuerza segun operator/name."""
    t = _norm(" ".join(tags.get(k, "") for k in ("operator", "name", "official_name", "brand")))
    words = set(re.findall(r"\w+", t))
    if "gendarmer" in t:
        return "GN", "Gendarmería Nacional"
    if "prefectura" in t:
        return "PNA", "Prefectura Naval Argentina"
    if "seguridad aeroportuaria" in t or "psa" in words:
        return "PSA", "Policía de Seguridad Aeroportuaria"
    if "rodoviaria federal" in t or "prf" in words:
        return "PRF", "Polícia Rodoviária Federal"
    if "policia federal" in t or "pfa" in words:
        return ("PF", "Polícia Federal") if country == "BR" else ("PFA", "Policía Federal Argentina")
    if "brigada militar" in t or "comando rodoviario" in t:
        return "BM", "Brigada Militar"
    if "rodoviaria" in t or "policia militar" in t:
        return "PM", "Polícia Militar Rodoviária" if "rodovi" in t else "Polícia Militar"
    if country == "BR" and ("policia civil" in t or "delegacia" in t):
        return "PC", "Polícia Civil"
    if "guarda municipal" in t or "guardia urbana" in t or "municipal" in words:
        return ("GM", "Guarda Municipal") if country == "BR" else ("MUN", "Policía municipal")
    if "patrulla caminera" in t:
        return "PC", "Patrulla Caminera"
    if "policia caminera" in t:
        return "PC", "Policía Caminera"
    if country == "BO" and "transito" in t:
        return "TR", "Tránsito"
    if country == "PY" and "policia nacional" in t:
        return "PN", "Policía Nacional"
    if any(w in t for w in ("policia", "police", "comisaria", "destacamento", "delegacia")):
        if country == "AR":
            return "POL", f"Policía de {region_name}"
        if country == "BR":
            return "PM", "Polícia Militar"
        if country == "BO":
            return "PB", "Policía Boliviana"
        return "PN", "Policía Nacional"
    return "CTRL", "Policía"


def is_police_post(tags):
    """Solo puestos de control reales en la calle o la ruta (no comisarias ni bases)."""
    if tags.get("barrier") == "checkpoint" or tags.get("police") == "checkpoint":
        return True
    if tags.get("amenity") == "police":
        t = _norm(" ".join(tags.get(k, "") for k in ("name", "operator", "police")))
        words = set(re.findall(r"\w+", t))
        if any(w in t for w in POLICE_NOT) or "base" in words:
            return False
        return any(w in t for w in POLICE_WORDS)
    return False

def is_police_station(tags):
    """Comisaria, base o destacamento (se ve en el mapa, sin alerta)."""
    return tags.get("amenity") == "police" and not is_police_post(tags)


# Marcas de estaciones de servicio con logo en la app (clave -> palabras a buscar).
FUEL_BRANDS = [
    ("ypf", ("ypf",)), ("shell", ("shell", "raizen")), ("axion", ("axion",)), ("puma", ("puma",)),
    ("gulf", ("gulf",)), ("refinor", ("refinor",)), ("dapsa", ("dapsa",)),
    ("petrobras", ("petrobras", "br distribuidora", "vibra", "br")), ("ipiranga", ("ipiranga",)),
    ("ale", ("ale", "alesat")), ("atem", ("atem",)), ("sim", ("sim",)), ("rodoil", ("rodoil",)),
    ("equador", ("equador",)), ("petropar", ("petropar",)), ("copetrol", ("copetrol",)), ("ancap", ("ancap",)),
]


def fuel_brand(tags):
    """(clave de la marca o "" si no hay logo, nombre para mostrar)."""
    t = _norm(" ".join(tags.get(k, "") for k in ("brand", "name", "operator")))
    words = set(re.findall(r"\w+", t))
    name = tags.get("name") or tags.get("brand") or ""
    if "ypfb" in words:
        return "", name or "YPFB"
    for key, keys in FUEL_BRANDS:
        if any((k in words) if (" " not in k and len(k) <= 4) else (k in t) for k in keys):
            return key, name
    return "", name



def toll_price(tags):
    """'1900 ARS' -> (1900.0, 'ARS'); None si no esta cargado."""
    import re as _re
    v = tags.get("charge") or tags.get("toll:charge") or tags.get("charge:motorcar") or ""
    m = _re.search(r"(\d+(?:[.,]\d+)?)\s*([A-Za-z]{3})?", v)
    if not m:
        return None
    return float(m.group(1).replace(",", ".")), (m.group(2) or "").upper()


def _dist(a, b):
    dy = (a[0] - b[0]) * 111320
    dx = (a[1] - b[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def dedupe(points, radius_m):
    """Uno por lugar: las cabinas de cada mano o varios nodos del mismo puesto."""
    out = []
    grid = {}
    for p in points:
        cy, cx = int(math.floor(p[0] * 111320 / 500)), int(math.floor(p[1] * 111320 / 500))
        near = [q for dy in (-1, 0, 1) for dx in (-1, 0, 1) for q in grid.get((cy + dy, cx + dx), [])]
        if any(_dist(p, q) < radius_m for q in near):
            continue
        out.append(p)
        grid.setdefault((cy, cx), []).append(p)
    return out


class Handler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.speed = []
        self.points = {}   # id -> [tipo, lat, lon, direccion, maxima]
        self.way_bearing = {}
        self.crossing_on_road = set()
        self.tolls = []    # [lat, lon, nombre, precio, moneda]
        self.police = []   # (lat, lon, tags)
        self.stations = []  # comisarias (lat, lon, tags)
        self.fuel = []     # estaciones de servicio [lat, lon, marca, nombre]
        self.places = []   # pueblos y ciudades (lon, lat, tipo)
        self.sig_ctx = {}  # semaforo -> [(ids de la calle, coords, indice, mano unica)] (para deducir su mano)
        self.no_speed = []  # rutas nacionales argentinas sin maxima: (numero, tipo, nombre, coords)
        self.bbox = [180.0, 90.0, -180.0, -90.0]

    def node(self, n):
        t = n.tags
        kind = None
        if t.get("highway") == "traffic_signals":
            kind = "signal"
        elif t.get("highway") == "speed_camera":
            kind = "radar"
        elif t.get("crossing") == "traffic_signals":
            # Cruce peatonal con semaforo (highway=crossing): tambien frena a
            # los autos si esta sobre una calle (se verifica en way()).
            kind = "crossing"
        if t.get("barrier") == "toll_booth":
            price = toll_price(t)
            self.tolls.append([round(n.location.lat, 6), round(n.location.lon, 6), t.get("name", ""),
                               price[0] if price else 0, price[1] if price else ""])
        if is_police_post(t):
            self.police.append((round(n.location.lat, 6), round(n.location.lon, 6), dict(t)))
        elif is_police_station(t):
            self.stations.append((round(n.location.lat, 6), round(n.location.lon, 6), dict(t)))
        if t.get("place") in official_speeds.URBAN_RADIUS_M:
            self.places.append((n.location.lon, n.location.lat, t.get("place")))
        if t.get("amenity") == "fuel":
            key, label = fuel_brand(t)
            self.fuel.append([round(n.location.lat, 6), round(n.location.lon, 6), key, label])
        if kind:
            self.points[n.id] = [kind, round(n.location.lat, 6), round(n.location.lon, 6),
                                 direction_of(t), parse_speed(t.get("maxspeed")) or 0]

    def way(self, w):
        # Puestos de control dibujados como area: su centro.
        # Comisarias y estaciones de servicio dibujadas como area: su centro.
        if w.is_closed() and (is_police_station(w.tags) or w.tags.get("amenity") == "fuel"):
            try:
                pts = [(nd.lat, nd.lon) for nd in w.nodes]
                la = round(sum(p[0] for p in pts) / len(pts), 6)
                lo = round(sum(p[1] for p in pts) / len(pts), 6)
                if w.tags.get("amenity") == "fuel":
                    key, label = fuel_brand(w.tags)
                    self.fuel.append([la, lo, key, label])
                else:
                    self.stations.append((la, lo, dict(w.tags)))
            except osmium.InvalidLocationError:
                pass
        if is_police_post(w.tags) and w.is_closed():
            try:
                pts = [(nd.lat, nd.lon) for nd in w.nodes]
                self.police.append((round(sum(p[0] for p in pts) / len(pts), 6),
                                    round(sum(p[1] for p in pts) / len(pts), 6), dict(w.tags)))
            except osmium.InvalidLocationError:
                pass
        hw = w.tags.get("highway")
        if hw not in ROADS:
            return
        try:
            coords = [(round(nd.lon, 6), round(nd.lat, 6)) for nd in w.nodes]
        except osmium.InvalidLocationError:
            return
        if len(coords) < 2:
            return
        for lon, lat in coords:
            b = self.bbox
            b[0], b[1], b[2], b[3] = min(b[0], lon), min(b[1], lat), max(b[2], lon), max(b[3], lat)
        ms = parse_speed(w.tags.get("maxspeed"))
        if ms:
            flat = [c for p in coords for c in p]
            self.speed.append([ms, hw, w.tags.get("name") or w.tags.get("ref") or "", flat])
        elif hw in ("motorway", "trunk", "primary", "secondary"):
            number = official_speeds.national_number(w.tags)
            if number and not official_speeds.looks_urban(w.tags):
                self.no_speed.append((number, hw, w.tags.get("name") or w.tags.get("ref") or "", coords))
        ow = oneway_of(w.tags)
        refs = None
        for i, nd in enumerate(w.nodes):
            if nd.ref in self.points and self.points[nd.ref][0] in ("signal", "crossing"):
                refs = refs or [n.ref for n in w.nodes]
                self.sig_ctx.setdefault(nd.ref, []).append((refs, coords, i, ow))
            if nd.ref in self.points and self.points[nd.ref][0] == "crossing":
                self.crossing_on_road.add(nd.ref)
            if nd.ref in self.points:
                a, b = (coords[i - 1], coords[i]) if i > 0 else (coords[0], coords[1])
                # Todas las calles que pasan por el punto, con su sentido.
                self.way_bearing.setdefault(nd.ref, []).append((bearing(a, b), ow))


def distance_m(lat1, lon1, lat2, lon2):
    dy = (lat1 - lat2) * 111320
    dx = (lon1 - lon2) * 111320 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


def oneway_of(t):
    """1 = mano unica a favor del dibujo, -1 = en contra, 0 = doble mano."""
    o = t.get("oneway", "")
    if o in ("yes", "true", "1"):
        return 1
    if o == "-1":
        return -1
    if o == "no":
        return 0
    if t.get("junction") in ("roundabout", "circular") or t.get("highway") in ("motorway", "motorway_link"):
        return 1
    return 0


def heading(point_id, direction, bearings):
    """
    Rumbo de los autos que ven el punto (-1 = todos). Con direction
    forward/backward, grados o punto cardinal se usa eso; si no, solo si esta
    sobre una calle de mano unica (en una esquina con varias calles no se sabe).
    """
    ways = bearings.get(point_id, [])
    if direction in ("forward", "backward") and ways:
        b = ways[0][0]
        return int(b if direction == "forward" else (b + 180) % 360)
    if direction:
        try:  # direccion en grados ("90") o punto cardinal
            cardinal = {"n": 0, "ne": 45, "e": 90, "se": 135, "s": 180, "sw": 225, "w": 270, "nw": 315}
            return int(cardinal[direction]) if direction in cardinal else int(float(direction)) % 360
        except ValueError:
            pass
    # Mano unica (la misma calle puede venir cortada en dos tramos justo ahi).
    if ways and all(o != 0 for _, o in ways):
        heads = [b if o == 1 else (b + 180) % 360 for b, o in ways]
        if all(min(abs(x - heads[0]), 360 - abs(x - heads[0])) < 45 for x in heads):
            return int(heads[0])
    return -1


WORK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "work")

# Regiones donde se deja solo lo de OpenStreetMap (ninguna por ahora).
OSM_ONLY = set()


def add_official_speeds(h, region_id, country):
    """Suma a h.speed los tramos de fuentes oficiales (quinto valor = fuente).

    - Brasil: placas ANTT y limites oficiales de Rio y Sao Paulo (via
      Pardal), con el sentido como sexto valor (-1 = los dos). La app los
      prefiere a los de OpenStreetMap en el mismo lugar.
    - Argentina: rutas nacionales sin maxima en OSM, con la de la ley segun
      el tipo de via del IGN (fuera de pueblos).
    Si una fuente no responde, el paquete sale igual.
    """
    if region_id in OSM_ONLY:
        return "solo OpenStreetMap"
    os.makedirs(WORK, exist_ok=True)
    try:
        if country == "BR":
            rows = official_speeds.brazil(region_id, WORK)
            for kmh, hw, source, direction, coords in rows:
                h.speed.append([kmh, hw or "trunk", "", [round(c, 6) for p in coords for c in p], source, direction])
            return f"{len(rows)} oficiales de Brasil"
        if country == "AR" and h.no_speed:
            routes = official_speeds.ArgentinaRoutes(WORK)
            rows = official_speeds.argentina_legal_rows(h.no_speed, routes, official_speeds.Towns(h.places))
            h.speed.extend(rows)
            return f"{len(rows)} de rutas nacionales con la maxima de la ley"
    except Exception as e:  # sin la fuente oficial, solo OpenStreetMap
        print("Sin velocidades oficiales:", e, flush=True)
    return "solo OpenStreetMap"


def main():
    pbf, out, region_id, name, country, date = sys.argv[1:7]
    h = Handler()
    h.apply_file(pbf, locations=True, idx="flex_mem")
    signals, radars, crossings = [], [], []
    # Semaforos "antes de la esquina" sin sentido: el de tu mano es el que va hacia la esquina.
    inferred = signal_dirs.infer(pbf, h.sig_ctx, ROADS)
    for pid, (kind, lat, lon, direction, maxspeed) in h.points.items():
        head = heading(pid, direction, h.way_bearing)
        if head == -1 and pid in inferred:
            head = inferred[pid]
        if kind == "signal":
            signals.append([lat, lon, head])
        elif kind == "radar":
            radars.append([lat, lon, maxspeed, head])
        elif pid in h.crossing_on_road:
            crossings.append((pid, lat, lon))
    # Cruces peatonales con semaforo sobre calles de autos: uno por esquina
    # (a menos de 35 m de otro semaforo ya contado, no se agrega).
    # Grilla de ~300 m para no comparar contra todos (regiones grandes).
    grid = {}

    def cell(lat, lon):
        return int(math.floor(lat * 370)), int(math.floor(lon * 370))

    for s in signals:
        grid.setdefault(cell(s[0], s[1]), []).append(s)
    added = 0
    for pid, lat, lon in sorted(crossings):
        cy, cx = cell(lat, lon)
        near = [s for dy in (-1, 0, 1) for dx in (-1, 0, 1) for s in grid.get((cy + dy, cx + dx), [])]
        if any(distance_m(lat, lon, s[0], s[1]) < 35 for s in near):
            continue
        head = heading(pid, "", h.way_bearing)
        point = [lat, lon, inferred.get(pid, head) if head == -1 else head]
        signals.append(point)
        grid.setdefault((cy, cx), []).append(point)
        added += 1
    tolls = dedupe(h.tolls, 300)
    police = []
    for lat, lon, tags in h.police:
        code, full = police_force(tags, country, name)
        police.append([lat, lon, code, full])
    police = dedupe(police, 150)
    stations = dedupe([[la, lo, *police_force(t, country, name)] for la, lo, t in h.stations], 80)
    fuel = dedupe(h.fuel, 40)
    official = add_official_speeds(h, region_id, country)
    data = {
        "id": region_id, "name": name, "country": country, "generated": date,
        "tolls": tolls, "police": police, "stations": stations, "fuel": fuel,
        "source": "© colaboradores de OpenStreetMap (ODbL)" + (
            "; limites oficiales ANTT / Rio / São Paulo via Pardal (ODbL)" if country == "BR" and region_id not in OSM_ONLY
            else "; tipo de via de rutas nacionales: IGN / Vialidad Nacional" if country == "AR" and region_id not in OSM_ONLY
            else ""),
        "bbox": [round(x, 4) for x in h.bbox],
        "speed": h.speed, "signals": signals, "radars": radars,
    }
    with open(out, "w") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    print(f"{region_id}: {len(h.speed)} tramos con maxima ({official}), {len(signals)} semaforos "
          f"({added} cruces peatonales), {len(radars)} radares, {len(tolls)} peajes, {len(police)} controles, {len(stations)} comisarías, {len(fuel)} estaciones de servicio")


if __name__ == "__main__":
    main()
