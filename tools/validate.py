"""CI: load every shipped conversion and check manifest + tools.json.  Exit 1 on any problem.

Run from the repo root:  PYTHONPATH=tests/fakecore python tools/validate.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

errors = []

# manifest
try:
    m = json.loads((ROOT / "manifest.json").read_text("utf-8"))
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(m.get("version", ""))):
        errors.append("manifest.json: version must be MAJOR.MINOR.PATCH")
    api = m.get("host_api", {})
    if not (isinstance(api.get("min"), int) and isinstance(api.get("max"), int) and api["min"] <= api["max"]):
        errors.append("manifest.json: host_api needs integer min <= max")
except Exception as e:  # noqa: BLE001
    errors.append("manifest.json: %s" % e)

# conversions (JSON and .py) - same loader the app uses, without the user folder
from convert.conversion import registry  # noqa: E402

registry.clear()
registry.load_dir(ROOT / "convert" / "conversions")
errors += registry.problems
print("%d conversion(s) loaded" % len(registry.all()))

ids = set()
for c in registry.all():
    if c.id in ids:
        errors.append("duplicate id " + c.id)
    ids.add(c.id)

# tools.json: every download needs a pinned hash or a sha256_url, and https
tools = json.loads((ROOT / "convert" / "tools.json").read_text("utf-8")).get("tools", {})
for name, t in tools.items():
    for plat, d in (t.get("download") or {}).items():
        where = "tools.json %s/%s" % (name, plat)
        if not str(d.get("url", "")).startswith("https://"):
            errors.append(where + ": url must be https")
        if not (d.get("sha256") or d.get("sha256_url")):
            errors.append(where + ": needs sha256 or sha256_url")

for e in errors:
    print("ERROR:", e)
sys.exit(1 if errors else 0)
