"""Mano de los semaforos cargados "antes de la esquina".

Muchos semaforos estan cargados en un punto suelto de la calle (no en la
esquina), donde frenan los autos, sin decir para que mano son
(traffic_signals:direction). En una calle de doble mano suele haber uno de
cada lado de la esquina: el de tu mano es el que esta ANTES de la esquina
(vas hacia ella); el que esta despues es el de la otra mano.

Para cada semaforo sin sentido, sobre una sola calle de doble mano y que no
es una esquina, se busca la esquina mas cercana (a menos de 100 m) a cada
lado sobre esa calle; si hay una sola (o una esta claramente mas cerca), el
semaforo vale para el transito que va hacia ella.

Esquina = nodo donde se juntan 3 o mas tramos de calle (una calle que solo
sigue en otro tramo no cuenta).
"""
import math

import osmium

NEAR_M = 100.0


def _dist(a, b):
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * 111320 * k, (b[1] - a[1]) * 110570)


def _bearing(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[1], a[0], b[1], b[0]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


class _Degrees(osmium.SimpleHandler):
    """Segunda pasada: cuantos tramos de calle salen de cada nodo candidato."""

    def __init__(self, need, roads):
        super().__init__()
        self.need = need
        self.roads = roads
        self.degree = {}

    def way(self, w):
        if w.tags.get("highway") not in self.roads:
            return
        refs = [n.ref for n in w.nodes]
        last = len(refs) - 1
        for i, r in enumerate(refs):
            if r in self.need:
                self.degree[r] = self.degree.get(r, 0) + (1 if i in (0, last) else 2)


def infer(pbf, contexts, roads):
    """contexts: {semaforo: [(ids de los nodos de la calle, coords (lon, lat), indice, mano unica)]}.

    Devuelve {semaforo: rumbo} para los que se pudo deducir.
    """
    cand = {}
    need = set()
    for pid, lst in contexts.items():
        if len(lst) != 1:
            continue  # en una esquina o entre dos calles: no se deduce
        ids, coords, i, oneway = lst[0]
        if oneway != 0 or i in (0, len(ids) - 1):
            continue
        cand[pid] = (ids, coords, i)
        for step in (1, -1):
            j, walked = i, 0.0
            while 0 <= j + step < len(ids) and walked <= NEAR_M:
                walked += _dist(coords[j], coords[j + step])
                j += step
                need.add(ids[j])
    if not cand:
        return {}
    deg = _Degrees(need, roads)
    deg.apply_file(pbf)
    out = {}
    for pid, (ids, coords, i) in cand.items():
        found = {}
        for step in (1, -1):
            j, walked = i, 0.0
            while 0 <= j + step < len(ids):
                walked += _dist(coords[j], coords[j + step])
                j += step
                if walked > NEAR_M:
                    break
                if deg.degree.get(ids[j], 0) >= 3:
                    found[step] = walked
                    break
        if not found:
            continue
        if len(found) == 2:
            near, far = sorted(found.items(), key=lambda kv: kv[1])
            if near[1] > far[1] * 0.5:
                continue  # entre dos esquinas parecidas: no se sabe
            step = near[0]
        else:
            step = next(iter(found))
        out[pid] = int(_bearing(coords[i], coords[i + step]))
    return out
