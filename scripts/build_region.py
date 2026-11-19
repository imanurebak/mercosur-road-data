"""Convierte un extracto .osm.pbf de OpenStreetMap en un paquete de region
para apps de navegacion: velocidades maximas (con trazado), semaforos y
radares, con el sentido que controlan cuando esta cargado.

Uso: python3 build_region.py extracto.osm.pbf salida.json ID NOMBRE PAIS FECHA
"""
import json
import math
import re
import sys

import osmium

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
    if "gendarmer" in t:
        return "GN", "Gendarmería Nacional"
    if "prefectura" in t:
        return "PNA", "Prefectura Naval Argentina"
    if "policia federal" in t or "pfa" in t.split():
        return "PFA", "Policía Federal Argentina"
    if "rodoviaria federal" in t or "prf" in t.split() or "prf " in t or " prf" in t:
        return "PRF", "Polícia Rodoviária Federal"
    if "brigada militar" in t or "comando rodoviario" in t:
        return "BM", "Brigada Militar"
    if "rodoviaria" in t or "policia militar" in t:
        return "PM", "Polícia Militar Rodoviária" if "rodovi" in t else "Polícia Militar"
    if "patrulla caminera" in t:
        return "PC", "Patrulla Caminera"
    if "policia caminera" in t:
        return "PC", "Policía Caminera"
    if country == "PY" and "policia nacional" in t:
        return "PN", "Policía Nacional"
    if "policia" in t or "policía" in t or "police" in t:
        if country == "AR":
            return "POL", f"Policía de {region_name}"
        if country == "BR":
            return "PM", "Polícia Militar"
        if country == "UY":
            return "PC", "Policía Caminera"
        return "PN", "Policía Nacional"
    return "CTRL", "Control policial"


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
        if kind:
            self.points[n.id] = [kind, round(n.location.lat, 6), round(n.location.lon, 6),
                                 direction_of(t), parse_speed(t.get("maxspeed")) or 0]

    def way(self, w):
        # Puestos de control dibujados como area: su centro.
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
        ow = oneway_of(w.tags)
        for i, nd in enumerate(w.nodes):
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


def main():
    pbf, out, region_id, name, country, date = sys.argv[1:7]
    h = Handler()
    h.apply_file(pbf, locations=True, idx="flex_mem")
    signals, radars, crossings = [], [], []
    for pid, (kind, lat, lon, direction, maxspeed) in h.points.items():
        head = heading(pid, direction, h.way_bearing)
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
        point = [lat, lon, heading(pid, "", h.way_bearing)]
        signals.append(point)
        grid.setdefault((cy, cx), []).append(point)
        added += 1
    tolls = dedupe(h.tolls, 300)
    police = []
    for lat, lon, tags in h.police:
        code, full = police_force(tags, country, name)
        police.append([lat, lon, code, full])
    police = dedupe(police, 150)
    data = {
        "id": region_id, "name": name, "country": country, "generated": date,
        "tolls": tolls, "police": police,
        "source": "© colaboradores de OpenStreetMap (ODbL)",
        "bbox": [round(x, 4) for x in h.bbox],
        "speed": h.speed, "signals": signals, "radars": radars,
    }
    with open(out, "w") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    print(f"{region_id}: {len(h.speed)} tramos con maxima, {len(signals)} semaforos "
          f"({added} cruces peatonales), {len(radars)} radares, {len(tolls)} peajes, {len(police)} controles")


if __name__ == "__main__":
    main()
