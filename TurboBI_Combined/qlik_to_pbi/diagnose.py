"""Print everything the direct QVF parser finds inside a .qvf.

Usage::

    python -m qlik_to_pbi.diagnose <path-to-qvf>

Lists app properties, load-script size, sheet count + titles + cell
counts, object / measure / dimension / variable counts, and prints the
first few sheets and objects in full so you can eyeball what survived
parsing. Handy when conversion produces unexpected output -- run this
first to see what the parser actually got from the QVF.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .qvf_direct import QvfParser


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("usage: python -m qlik_to_pbi.diagnose <path-to-qvf>",
              file=sys.stderr)
        return 2

    path = Path(argv[0])
    if not path.is_file():
        print(f"QVF not found: {path}", file=sys.stderr)
        return 1

    header = path.read_bytes()[:32]
    print(f"File: {path.name}  ({path.stat().st_size:,} bytes)")
    print(f"Header hex: {header.hex()}")
    print()

    try:
        c = QvfParser(str(path)).parse()
    except Exception as exc:
        print(f"PARSE ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"app_properties keys : {list(c.app_properties.keys())[:10]}")
    print(f"load_script length  : {len(c.load_script):,} chars")
    print(f"sheets              : {len(c.sheets)}")
    print(f"objects             : {len(c.objects)}")
    print(f"measures            : {len(c.measures)}")
    print(f"dimensions          : {len(c.dimensions)}")
    print(f"variables           : {len(c.variables)}")
    print(f"raw_file_map keys   : {list(c.raw_file_map.keys())[:20]}")
    print()

    if c.load_script:
        # Slice + encode/decode to scrub any console-incompatible glyphs
        # (Qlik scripts sometimes carry stray combining marks that
        # cp1252 can't render).
        preview = c.load_script[:200].encode(
            sys.stdout.encoding or "utf-8", errors="replace",
        ).decode(sys.stdout.encoding or "utf-8", errors="replace")
        print(f"load_script preview : {preview!r}")
    else:
        print("load_script preview : (empty)")
    print()

    for i, s in enumerate(c.sheets):
        meta = (
            s.get("qMetaDef")
            or (s.get("qProperty") or {}).get("qMetaDef")
            or {}
        )
        title = meta.get("title") or "?"
        cells = s.get("cells") or (s.get("qProperty") or {}).get("cells") or []
        cell_names = [cl.get("name") for cl in cells[:5]]
        print(
            f"Sheet {i}: {title!r} -- {len(cells)} cells: {cell_names}"
        )
    print()

    for i, s in enumerate(c.sheets[:3]):
        print(f"=== SHEET {i} ===")
        print(json.dumps(s, indent=2)[:400])
        print()

    for i, o in enumerate(c.objects[:3]):
        print(f"=== OBJECT {i} ===")
        print(json.dumps(o, indent=2)[:400])
        print()

    for i, m in enumerate(c.measures[:3]):
        print(f"=== MEASURE {i} ===")
        print(json.dumps(m, indent=2)[:400])
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
