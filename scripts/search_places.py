"""Lugares con nombre para buscar SIN internet (ciudades, pueblos, barrios y
lugares conocidos: hospitales, bancos, supermercados, hoteles, terminales…).

Cada lugar: [lat, lon, nombre, tipo]. El tipo es el valor de OSM ("city",
"town", "hospital", "supermarket"…), para ordenar (la ciudad primero) y
mostrar.
"""
PLACES = {"city", "town", "municipality", "village", "hamlet", "suburb", "neighbourhood", "quarter", "locality"}
AMENITY = {"hospital", "clinic", "pharmacy", "bank", "school", "university", "college", "police", "townhall",
           "bus_station", "restaurant", "cafe", "fast_food", "marketplace", "place_of_worship", "library",
           "courthouse", "post_office", "fire_station", "cinema", "theatre", "community_centre", "ice_cream",
           "car_wash", "doctors", "dentist", "veterinary", "kindergarten", "bar", "pub", "nightclub"}
SHOP = {"supermarket", "mall", "department_store", "hardware", "doityourself", "car_repair", "car_parts",
        "tyres", "bakery", "butcher", "convenience", "electronics", "clothes", "furniture", "optician", "mobile_phone"}
TOURISM = {"hotel", "motel", "hostel", "guest_house", "attraction", "museum", "viewpoint", "camp_site", "chalet"}
LEISURE = {"park", "stadium", "sports_centre", "water_park"}


def kind_of(tags):
    """Tipo del lugar (si se busca por nombre) o None."""
    if not tags.get("name"):
        return None
    for key, values in (("place", PLACES), ("amenity", AMENITY), ("shop", SHOP), ("tourism", TOURISM),
                        ("leisure", LEISURE)):
        v = tags.get(key)
        if v in values:
            return v
    if tags.get("aeroway") == "aerodrome":
        return "aerodrome"
    if tags.get("railway") == "station":
        return "station"
    if tags.get("office") == "government":
        return "government"
    return None


def dedupe(items, dist_m):
    """Mismo nombre a menos de `dist_m`: uno solo (el nodo y el area del mismo lugar)."""
    import math
    seen = {}
    out = []
    for it in items:
        key = it[2].lower()
        cell = (round(it[0] * 200), round(it[1] * 200))
        dup = False
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for o in seen.get((key, cell[0] + dy, cell[1] + dx), ()):
                    k = math.cos(math.radians(it[0]))
                    if math.hypot((o[0] - it[0]) * 110570, (o[1] - it[1]) * 111320 * k) < dist_m:
                        dup = True
        if not dup:
            out.append(it)
            seen.setdefault((key, cell[0], cell[1]), []).append(it)
    return out


# ---- Calles con nombre (sin numero) ----

TOWN_KINDS = {"city", "town", "municipality", "village", "hamlet"}


class Streets:
    """Junta las calles con nombre: un punto por calle y pueblo (el tramo del
    medio de la calle), para buscarlas sin internet ("Almafuerte, San Vicente")."""

    def __init__(self):
        self.ways = {}   # nombre -> [(lat, lon) del medio de cada tramo]
        self.towns = []  # (lat, lon, nombre)

    def add_town(self, tags, lat, lon):
        if tags.get("place") in TOWN_KINDS and tags.get("name"):
            self.towns.append((lat, lon, tags.get("name")))

    def add_way(self, name, coords):
        """coords: [(lon, lat), ...] de un tramo con nombre."""
        if not name or len(coords) < 2:
            return
        lon, lat = coords[len(coords) // 2]
        self.ways.setdefault(name, []).append((lat, lon))

    def rows(self, max_km=15.0):
        """[lat, lon, nombre, "street", pueblo] por cada calle y pueblo."""
        import math
        grid = {}
        for la, lo, nm in self.towns:
            grid.setdefault((int(math.floor(la * 10)), int(math.floor(lo * 10))), []).append((la, lo, nm))

        def town_at(la, lo):
            best, best_d = "", max_km * 1000
            cy, cx = int(math.floor(la * 10)), int(math.floor(lo * 10))
            for dy in (-2, -1, 0, 1, 2):
                for dx in (-2, -1, 0, 1, 2):
                    for tla, tlo, nm in grid.get((cy + dy, cx + dx), ()):
                        d = math.hypot((tla - la) * 110570, (tlo - lo) * 111320 * math.cos(math.radians(la)))
                        if d < best_d:
                            best, best_d = nm, d
            return best

        out = []
        for name, mids in self.ways.items():
            groups = {}
            for la, lo in mids:
                groups.setdefault(town_at(la, lo), []).append((la, lo))
            for town, pts in groups.items():
                # El tramo mas cerca del centro de todos los de esa calle en ese pueblo.
                mla = sum(p[0] for p in pts) / len(pts)
                mlo = sum(p[1] for p in pts) / len(pts)
                la, lo = min(pts, key=lambda p: (p[0] - mla) ** 2 + (p[1] - mlo) ** 2)
                out.append([round(la, 6), round(lo, 6), name, "street", town])
        return out
