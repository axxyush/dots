#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "floorplan_parser"))

from roomplan_to_layout_2d import roomplan_json_to_layout_2d  # noqa: E402
from connectdots_pdf import generate_tactile_pdf  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", required=True, help="Path to RoomPlan JSON file")
    ap.add_argument("--out", dest="out_pdf", required=True, help="Output PDF path")
    args = ap.parse_args()

    rp = json.loads(Path(args.in_path).read_text(encoding="utf-8"))
    layout, metadata = roomplan_json_to_layout_2d(rp)

    generate_tactile_pdf(
        output_pdf_path=str(Path(args.out_pdf)),
        layout=layout,
        metadata=metadata,
        room_id=Path(args.in_path).stem,
        number_objects=False,
        include_legend_page=False,
    )
    print(str(Path(args.out_pdf)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

