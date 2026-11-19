"""Genera todos los paquetes de regions.json en out/ (comprimidos .json.gz)
y el catalogo out/index.json. Uso: python3 scripts/build_all.py [id ...]"""
import datetime
import gzip
import json
import os
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
WORK = os.path.join(ROOT, "work")
RELEASE = "https://github.com/imanurebak/mercosur-road-data/releases/download/data/"


def main():
    catalog = json.load(open(os.path.join(ROOT, "regions.json"), encoding="utf-8"))
    only = set(sys.argv[1:])
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)
    date = datetime.date.today().isoformat()
    index = {"generated": date, "countries": catalog["countries"], "regions": []}
    for region in catalog["regions"]:
        if only and region["id"] not in only:
            continue
        pbf = os.path.join(WORK, region["id"] + ".osm.pbf")
        raw = os.path.join(WORK, region["id"] + ".json")
        packed = os.path.join(OUT, region["id"] + ".json.gz")
        try:
            print("Bajando", region["id"], flush=True)
            req = urllib.request.Request(region["url"], headers={"User-Agent": "mercosur-road-data"})
            with urllib.request.urlopen(req, timeout=600) as r, open(pbf, "wb") as f:
                shutil.copyfileobj(r, f)
            subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_region.py"), pbf, raw,
                            region["id"], region["name"], region["country"], date], check=True)
            with open(raw, "rb") as src, gzip.open(packed, "wb", compresslevel=9) as dst:
                shutil.copyfileobj(src, dst)
            info = json.load(open(raw, encoding="utf-8"))
            # Red de calles para calcular rutas sin conexion (si falla, la region sale igual).
            graph = os.path.join(OUT, region["id"] + ".graph.gz")
            try:
                subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_graph.py"), pbf, graph], check=True)
            except Exception as e:
                print("ERROR en la red de calles de", region["id"], e, flush=True)
                graph = None
            index["regions"].append({
                "id": region["id"], "name": region["name"], "country": region["country"],
                "generated": date, "bbox": info["bbox"], "bytes": os.path.getsize(packed),
                "speed": len(info["speed"]), "signals": len(info["signals"]), "radars": len(info["radars"]),
                "tolls": len(info.get("tolls", [])), "police": len(info.get("police", [])),
                "url": RELEASE + region["id"] + ".json.gz",
                **({"graphUrl": RELEASE + region["id"] + ".graph.gz", "graphBytes": os.path.getsize(graph)}
                   if graph and os.path.exists(graph) else {}),
            })
        except Exception as e:  # una region que falla no frena las demas
            print("ERROR en", region["id"], e, flush=True)
        finally:
            for path in (pbf, raw):
                if os.path.exists(path):
                    os.remove(path)
    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    print("Listo:", len(index["regions"]), "regiones")


if __name__ == "__main__":
    main()
