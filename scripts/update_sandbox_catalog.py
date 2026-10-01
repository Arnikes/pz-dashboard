"""Rebuild the B42 field/type inventory from the developer's Java API docs.

This inventories names/types only. Installed profile comments provide version
specific bounds; translated labels come from the installed game, not this site.
"""

import argparse
import json
import re
import urllib.request
from html import unescape
from pathlib import Path

SOURCE = "https://projectzomboid.com/modding/zombie/SandboxOptions"
PAGES = {
    "": "sandbox-api.html",
    "ZombieLore": "sandbox-zombielore.html",
    "ZombieConfig": "sandbox-zombieconfig.html",
    "Map": "sandbox-map.html",
    "MultiplierConfig": "sandbox-multiplier.html",
    "Basement": "sandbox-basement.html",
}


def inventory(html):
    summary = html.split('id="field-summary"', 1)[1].split("</section>", 1)[0]
    pairs = re.findall(
        r'col-first[^>]*>(.*?)</div>\s*<div class="col-second[^>]*>.*?class="member-name-link">([^<]+)',
        summary,
        re.S,
    )
    result = {}
    for declared, name in pairs:
        kind = re.search(
            r"SandboxOptions\.(Boolean|Integer|Double|String|Enum|StrongEnum)SandboxOption",
            unescape(declared),
        )
        if kind:
            result[name.casefold()] = {
                "Boolean": "boolean",
                "Integer": "integer",
                "Double": "double",
                "String": "string",
                "Enum": "enum",
                "StrongEnum": "enum",
            }[kind[1]]
    if not result:
        raise ValueError("API field table was not recognized")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, help="Read already saved primary-source HTML")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "dashboard/schemas/b42-api.json",
    )
    args = parser.parse_args()
    catalog = {"formatVersion": 1, "family": "B42", "source": SOURCE + ".html", "tables": {}}
    for table, filename in PAGES.items():
        if args.input_dir:
            html = (args.input_dir / filename).read_text(encoding="utf-8")
        else:
            url = SOURCE + ("." + table if table else "") + ".html"
            with urllib.request.urlopen(url, timeout=30) as response:
                html = response.read(2_000_000).decode("utf-8")
        catalog["tables"][table] = inventory(html)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(sum(len(fields) for fields in catalog["tables"].values()), "stock fields inventoried")


if __name__ == "__main__":
    main()
