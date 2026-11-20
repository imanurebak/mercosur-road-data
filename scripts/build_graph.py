#!/usr/bin/env python3
"""
Red de calles para calcular rutas SIN CONEXION en Auto Hub (etapa 1).

Lee un extracto .osm.pbf y arma un grafo compacto: los nodos son las esquinas
(donde se cruzan calles) y los extremos de cada calle; cada tramo entre dos
esquinas guarda largo, velocidad, sentido, tipo de calle, si es de tierra, si
tiene peaje, si es rotonda, el nombre y el dibujo (para mostrarlo en el mapa
y armar las indicaciones de giro).

Uso: build_graph.py region.osm.pbf salida.graph.gz [principales.main.graph.gz]

Formato "AHG1" (little endian, todo dentro de un gzip):
  cabecera: b"AHG1", version u16, nodos u32, tramos u32, nombres u32,
            restricciones u32, minLon/minLat/maxLon/maxLat i32 (1e-6 grados)
  nodos:    lat i32 y lon i32 (1e-6 grados) de cada uno, ordenados por id OSM;
            luego los ids OSM (varint, diferencia con el anterior), para unir
            regiones vecinas por los nodos compartidos del limite
  nombres:  cantidad de bytes u32 + textos UTF-8 separados por \\0
  tramos:   (varints) desde, hasta, largo en decimetros, velocidad km/h,
            banderas, tipo, nombre (indice+1, 0 = sin nombre), cantidad de
            puntos intermedios y sus diferencias (zigzag, 1e-6 grados)
  restricciones de giro (varints): tramo desde, nodo via, tramo hasta, tipo
            (0 = prohibido, 1 = obligatorio "only_")

Banderas: 1 mano unica (sentido del dibujo), 2 mano unica al reves, 4 tierra,
8 peaje, 16 autopista, 32 rotonda, 64 ferry, 128 acceso limitado (destination).
"""
import gzip
import math
import struct
import sys

import osmium

# Tipos de calle por donde va un auto, con su codigo y velocidad tipica (km/h)
# cuando no tiene maxima cargada.
CLASSES = {
    "motorway": (0, 110), "motorway_link": (1, 60), "trunk": (2, 90), "trunk_link": (3, 50),
    "primary": (4, 70), "primary_link": (5, 45), "secondary": (6, 60), "secondary_link": (7, 40),
    "tertiary": (8, 50), "tertiary_link": (9, 35), "unclassified": (10, 40), "residential": (11, 30),
    "living_street": (12, 15), "service": (13, 20), "road": (14, 35), "track": (15, 25),
}
UNPAVED = {"unpaved", "dirt", "earth", "ground", "gravel", "fine_gravel", "compacted", "sand", "mud", "grass",
           "pebblestone", "rock", "clay", "laterite"}
NO_ACCESS = {"no", "private", "agricultural", "forestry", "delivery"}


def parse_speed(v):
    if not v:
        return None
    import re
    m = re.search(r"\d+", v)
    if not m:
        return None
    n = int(m.group())
    return int(n * 1.609) if "mph" in v else n


def car_allowed(t):
    hw = t.get("highway")
    if t.get("route") == "ferry":
        return t.get("motor_vehicle") not in NO_ACCESS and t.get("motorcar") not in NO_ACCESS
    if hw not in CLASSES:
        return False
    if hw == "service" and t.get("service") in ("parking_aisle", "drive-through", "emergency_access"):
        return False
    if t.get("area") == "yes":
        return False
    for k in ("motorcar", "motor_vehicle", "vehicle", "access"):
        v = t.get(k)
        if v is None:
            continue
        if v in ("yes", "designated", "permissive", "destination", "customers"):
            return True
        if v in NO_ACCESS:
            return False
    if hw == "track":
        # Caminos de tierra rurales: si, salvo que esten marcados como solo a pie o en muy mal estado.
        return t.get("tracktype") not in ("grade5",) and t.get("smoothness") not in ("impassable", "very_horrible")
    return True


def oneway_of(t):
    o = t.get("oneway", "")
    if o in ("yes", "true", "1"):
        return 1
    if o == "-1":
        return -1
    if o in ("no", "reversible", "alternating"):
        return 0
    if t.get("junction") in ("roundabout", "circular") or t.get("highway") in ("motorway", "motorway_link"):
        return 1
    return 0


class Ways(osmium.SimpleHandler):
    """Primera pasada: calles para autos (ids de sus nodos) y restricciones de giro."""

    def __init__(self):
        super().__init__()
        self.ways = []          # (id, [nodos], tags utiles)
        self.use = {}           # nodo -> cuantas veces aparece (para saber cuales son esquinas)
        self.restrictions = []  # (via nodo, desde way, hasta way, tipo)

    def way(self, w):
        t = w.tags
        if not car_allowed(t):
            return
        nodes = [n.ref for n in w.nodes]
        if len(nodes) < 2:
            return
        keep = {k: t.get(k) for k in ("highway", "maxspeed", "surface", "tracktype", "toll", "junction", "name",
                                       "ref", "route", "access", "motor_vehicle", "duration", "oneway")
                if t.get(k) is not None}
        self.ways.append((w.id, nodes, keep))
        for i, n in enumerate(nodes):
            # Los extremos cuentan doble: siempre son nodos del grafo.
            self.use[n] = self.use.get(n, 0) + (2 if i in (0, len(nodes) - 1) else 1)

    def relation(self, r):
        t = r.tags
        if t.get("type") != "restriction":
            return
        kind = t.get("restriction") or t.get("restriction:motorcar") or ""
        if not (kind.startswith("no_") or kind.startswith("only_")):
            return
        frm = via = to = None
        for m in r.members:
            if m.role == "from" and m.type == "w":
                frm = m.ref
            elif m.role == "to" and m.type == "w":
                to = m.ref
            elif m.role == "via" and m.type == "n":
                via = m.ref
        if frm and via and to:
            self.restrictions.append((via, frm, to, 1 if kind.startswith("only_") else 0))


class Coords(osmium.SimpleHandler):
    """Segunda pasada: coordenadas de los nodos usados."""

    def __init__(self, wanted):
        super().__init__()
        self.wanted = wanted
        self.loc = {}

    def node(self, n):
        if n.id in self.wanted:
            self.loc[n.id] = (round(n.location.lat * 1e6), round(n.location.lon * 1e6))


def varint(out, v):
    v = int(v)
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)


def zigzag(v):
    return (v << 1) ^ (v >> 63)


def dist_m(a, b):
    la1, lo1 = a[0] / 1e6, a[1] / 1e6
    la2, lo2 = b[0] / 1e6, b[1] / 1e6
    dy = (la2 - la1) * 111_320
    dx = (lo2 - lo1) * 111_320 * math.cos(math.radians((la1 + la2) / 2))
    return math.hypot(dx, dy)


def main():
    pbf, out_path = sys.argv[1], sys.argv[2]
    ways = Ways()
    ways.apply_file(pbf)
    coords = Coords(set(ways.use))
    coords.apply_file(pbf)
    loc = coords.loc

    # Nodos del grafo: esquinas (usados mas de una vez) y extremos.
    graph_ids = sorted(n for n, c in ways.use.items() if c >= 2 and n in loc)
    index = {n: i for i, n in enumerate(graph_ids)}

    names, name_index = [], {}

    def name_id(t):
        label = t.get("name") or t.get("ref") or ""
        if t.get("name") and t.get("ref"):
            label = f"{t['name']}\t{t['ref']}"
        if not label:
            return 0
        if label not in name_index:
            name_index[label] = len(names)
            names.append(label)
        return name_index[label] + 1

    edges = []          # (desde, hasta, largo dm, km/h, banderas, tipo, nombre, puntos intermedios)
    way_edges = {}      # way id -> [(indice de tramo, nodo desde, nodo hasta)] para las restricciones
    for wid, nodes, t in ways.ways:
        hw = t.get("highway")
        cls, typical = CLASSES.get(hw, (16, 20)) if t.get("route") != "ferry" else (16, 15)
        kmh = parse_speed(t.get("maxspeed")) or typical
        flags = 0
        ow = oneway_of(t)
        if ow == 1:
            flags |= 1
        elif ow == -1:
            flags |= 2
        if t.get("surface") in UNPAVED or (hw == "track" and t.get("surface") is None):
            flags |= 4
        if t.get("toll") == "yes":
            flags |= 8
        if hw in ("motorway", "motorway_link"):
            flags |= 16
        if t.get("junction") in ("roundabout", "circular"):
            flags |= 32
        if t.get("route") == "ferry":
            flags |= 64
        if t.get("access") == "destination" or t.get("motor_vehicle") == "destination":
            flags |= 128
        nid = name_id(t)
        start = None
        shape = []
        length = 0.0
        prev = None
        for n in nodes:
            p = loc.get(n)
            if p is None:
                # Nodo fuera del extracto: se corta la calle ahi.
                start, shape, length, prev = None, [], 0.0, None
                continue
            if prev is not None:
                length += dist_m(prev, p)
            prev = p
            if n in index:
                if start is not None and start != n:
                    edges.append((index[start], index[n], max(1, round(length * 10)), kmh, flags, cls, nid, shape))
                    way_edges.setdefault(wid, []).append((len(edges) - 1, start, n))
                start, shape, length = n, [], 0.0
            elif start is not None:
                shape.append(p)

    # Restricciones de giro: tramo de la calle "desde" que llega al nodo via y
    # tramo de la calle "hasta" que sale de el.
    restrictions = []
    for via, frm, to, kind in ways.restrictions:
        if via not in index:
            continue
        e_from = next((e for e, a, b in way_edges.get(frm, []) if via in (a, b)), None)
        e_to = next((e for e, a, b in way_edges.get(to, []) if via in (a, b)), None)
        if e_from is not None and e_to is not None:
            restrictions.append((e_from, index[via], e_to, kind))

    write(out_path, graph_ids, loc, names, edges, restrictions)
    # Version chica: solo rutas y avenidas principales (autopista a terciaria)
    # y balsas, para las regiones del medio de un viaje largo (se lee rapido).
    if len(sys.argv) > 3:
        main_edges, remap_edge = [], {}
        for i, e in enumerate(edges):
            if e[5] <= 9 or e[5] == 16:
                remap_edge[i] = len(main_edges)
                main_edges.append(e)
        used = sorted({e[0] for e in main_edges} | {e[1] for e in main_edges})
        new_index = {old: new for new, old in enumerate(used)}
        main_ids = [graph_ids[i] for i in used]
        main_edges = [(new_index[a], new_index[b], dm, kmh, fl, cls, nid, shape)
                      for a, b, dm, kmh, fl, cls, nid, shape in main_edges]
        main_restr = [(remap_edge[f], new_index[v], remap_edge[t], k) for f, v, t, k in restrictions
                      if f in remap_edge and t in remap_edge and v in new_index]
        write(sys.argv[3], main_ids, loc, names, main_edges, main_restr)


def write(out_path, graph_ids, loc, names, edges, restrictions):
    lats = [loc[n][0] for n in graph_ids]
    lons = [loc[n][1] for n in graph_ids]
    body = bytearray()
    body += b"AHG1"
    body += struct.pack("<HIIII", 1, len(graph_ids), len(edges), len(names), len(restrictions))
    body += struct.pack("<iiii", min(lons), min(lats), max(lons), max(lats))
    for n in graph_ids:
        body += struct.pack("<ii", loc[n][0], loc[n][1])
    prev = 0
    for n in graph_ids:
        varint(body, n - prev)
        prev = n
    text = "\0".join(names).encode("utf-8")
    body += struct.pack("<I", len(text))
    body += text
    for a, b, dm, kmh, flags, cls, nid, shape in edges:
        for v in (a, b, dm, kmh, flags, cls, nid, len(shape)):
            varint(body, v)
        last = (lats[a], lons[a])
        for p in shape:
            varint(body, zigzag(p[0] - last[0]))
            varint(body, zigzag(p[1] - last[1]))
            last = p
    for e_from, via, e_to, kind in restrictions:
        for v in (e_from, via, e_to, kind):
            varint(body, v)
    with gzip.open(out_path, "wb", compresslevel=9) as f:
        f.write(bytes(body))

    total_km = sum(e[2] for e in edges) / 10_000
    print(f"{out_path}: {len(graph_ids)} nodos, {len(edges)} tramos ({total_km:.0f} km), {len(names)} nombres, "
          f"{len(restrictions)} restricciones de giro, {len(body) / 1e6:.1f} MB sin comprimir")


if __name__ == "__main__":
    main()
