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
        self.bbox = [180.0, 90.0, -180.0, -90.0]

    def node(self, n):
        t = n.tags
        kind = None
        if t.get("highway") == "traffic_signals":
            kind = "signal"
        elif t.get("highway") == "speed_camera":
            kind = "radar"
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
            if nd.ref in self.points and nd.ref not in self.way_bearing:
                a, b = (coords[i - 1], coords[i]) if i > 0 else (coords[0], coords[1])
                self.way_bearing[nd.ref] = bearing(a, b)


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
    signals, radars = [], []
    for pid, (kind, lat, lon, direction, maxspeed) in h.points.items():
        head = heading(pid, direction, h.way_bearing)
        if kind == "signal":
            signals.append([lat, lon, head])
        else:
            radars.append([lat, lon, maxspeed, head])
    data = {
        "id": region_id, "name": name, "country": country, "generated": date,
        "source": "© colaboradores de OpenStreetMap (ODbL)",
        "bbox": [round(x, 4) for x in h.bbox],
        "speed": h.speed, "signals": signals, "radars": radars,
    }
    with open(out, "w") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    print(f"{region_id}: {len(h.speed)} tramos con maxima, {len(signals)} semaforos, {len(radars)} radares")


if __name__ == "__main__":
    main()
