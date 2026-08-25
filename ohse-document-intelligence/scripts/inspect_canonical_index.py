import json
from pathlib import Path

path = Path("data/canonical/grids/2b0822424448_46-70/index.json")

with path.open("r", encoding="utf-8") as f:
    data = json.load(f)

print("TYPE:", type(data).__name__)

if isinstance(data, dict):
    print("\nTOP-LEVEL KEYS:")
    for key in data.keys():
        print(" -", key)

print("\nFULL STRUCTURE:")
print(json.dumps(data, ensure_ascii=False, indent=2))