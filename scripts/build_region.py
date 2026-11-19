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


class Handler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.speed = []
        self.points = {}   # id -> [tipo, lat, lon, direccion, maxima]
        self.way_bearing = {}
        self.crossing_on_road = set()
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
        if kind:
            self.points[n.id] = [kind, round(n.location.lat, 6), round(n.location.lon, 6),
                                 direction_of(t), parse_speed(t.get("maxspeed")) or 0]

    def way(self, w):
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
        for i, nd in enumerate(w.nodes):
            if nd.ref in self.points and self.points[nd.ref][0] == "crossing":
                self.crossing_on_road.add(nd.ref)
            if nd.ref in self.points and nd.ref not in self.way_bearing:
                a, b = (coords[i - 1], coords[i]) if i > 0 else (coords[0], coords[1])
                self.way_bearing[nd.ref] = bearing(a, b)


def distance_m(lat1, lon1, lat2, lon2):
    dy = (lat1 - lat2) * 111320
    dx = (lon1 - lon2) * 111320 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


def heading(point_id, direction, bearings):
    if direction in ("forward", "backward") and point_id in bearings:
        b = bearings[point_id]
        return int(b if direction == "forward" else (b + 180) % 360)
    try:  # direccion en grados ("90") o punto cardinal
        cardinal = {"n": 0, "ne": 45, "e": 90, "se": 135, "s": 180, "sw": 225, "w": 270, "nw": 315}
        return int(cardinal[direction]) if direction in cardinal else int(float(direction)) % 360
    except ValueError:
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
    for _, lat, lon in sorted(crossings):
        cy, cx = cell(lat, lon)
        near = [s for dy in (-1, 0, 1) for dx in (-1, 0, 1) for s in grid.get((cy + dy, cx + dx), [])]
        if any(distance_m(lat, lon, s[0], s[1]) < 35 for s in near):
            continue
        point = [lat, lon, -1]
        signals.append(point)
        grid.setdefault((cy, cx), []).append(point)
        added += 1
    data = {
        "id": region_id, "name": name, "country": country, "generated": date,
        "source": "© colaboradores de OpenStreetMap (ODbL)",
        "bbox": [round(x, 4) for x in h.bbox],
        "speed": h.speed, "signals": signals, "radars": radars,
    }
    with open(out, "w") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    print(f"{region_id}: {len(h.speed)} tramos con maxima, {len(signals)} semaforos "
          f"({added} cruces peatonales), {len(radars)} radares")


if __name__ == "__main__":
    main()
