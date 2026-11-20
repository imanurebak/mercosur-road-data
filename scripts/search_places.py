"""Lugares con nombre para buscar SIN internet (ciudades, pueblos, barrios y
lugares conocidos: hospitales, bancos, supermercados, hoteles, terminales…).

Cada lugar: [lat, lon, nombre, tipo]. El tipo es el valor de OSM ("city",
"town", "hospital", "supermarket"…), para ordenar (la ciudad primero) y
mostrar.
"""
PLACES = {"city", "town", "village", "hamlet", "suburb", "neighbourhood", "quarter", "locality"}
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
