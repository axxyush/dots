# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

SenseGrid turns one or more photos of the **same room** into a tactile Braille-like ASCII map. All runnable code lives in `braille_local/`. The YOLO weights (`yolov8n.pt`) and sample photos (`room_photos/`, `top_level/`) are committed at the repo root.

## Setup & commands

```bash
# one-time
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then paste GEMINI_API_KEY

# run (multi-view, default "architecture" pipeline)
cd braille_local
python main.py ../room_photos                      # folder of images (non-recursive)
python main.py ../room_photos/IMG_5528.jpeg ../room_photos/IMG_5529.jpeg
python main.py ../top_level/room-image.png --pipeline legacy
```

Key flags: `--pipeline {depth,architecture,legacy}` (depth is default), `--grid-size {10,20}`, `--yolo-conf 0.35`, `--cluster-dist 0.34`, `--vision {auto,gemini,yolo}` (legacy only), `--gemini-model gemini-2.5-flash`.

There is no test suite, linter config, or build step.

## Architecture

Three pipelines feed the same rendering/Braille stage. All produce a list of grid **placements** (rectangles with a symbol) that `render_map.rasterize` paints and `braille.tactile_grid_to_braille` encodes.

**Pipeline 0 — `depth` (default, `_run_depth_pipeline` in `main.py`):**
The most accurate pipeline. Fixes the core geometric problem with perspective photos: pixel-y position is *not* room depth.
1. `detect.yolo_detections_with_depth` — YOLOv8 per image + Depth Anything V2 Small (via `transformers`) depth map per image. For each detected bbox: (a) sample the depth map at the **floor contact point** (bottom-center of bbox); (b) apply pinhole-camera perspective correction to the horizontal position; (c) use depth as the top-down Y coordinate. Falls back to a "bottom-edge-y as depth proxy" heuristic (still better than architecture pipeline) when `transformers` is not installed.
2. `layout.build_room_layout` — same as architecture pipeline.
3. `render_map.placements_from_norm_regions` — same as architecture pipeline.

Depth estimation lives in `depth.py`: `estimate_depth()` auto-detects the disparity-vs-depth convention by comparing mean values of bottom rows (floor, should be close) vs top rows (ceiling/back wall, should be far). `bbox_to_topdown()` does the pinhole projection math.

**Pipeline 1 — `architecture` (`_run_architecture` in `main.py`):**
1. `detect.yolo_detections_multiview` — run YOLOv8 per image; keep boxes above `--yolo-conf`, drop COCO "room noise" labels (`_YOLO_ROOM_NOISE`). Emits normalized `norm_x/y/x1..y2` plus `view` index and `conf`.
2. `layout.build_room_layout` — drop `_IGNORE_NAMES` (person, vehicles, animals), merge same-class detections across views via union-find with `cluster_dist` radius over normalized centers, then `apply_constraints` caps instances per bucket (`_BUCKET_MAX`: bed=1, chair=1, couch=1, table=2, others default 2), keeping highest-confidence.
3. `render_map.placements_from_norm_regions` → `norm_regions_to_grid` rasterizes the merged normalized bboxes onto a `grid_size × grid_size` integer grid.

**Pipeline 2 — `legacy` (`_run_legacy` + `spatial.create_spatial_map`):**
1. `detect.detect_objects_many` — per image, Gemini vision returns JSON (name + pixel center + bbox); YOLO is a fallback when Gemini is empty or there's no API key. `utils.extract_json_array_lenient` salvages truncated Gemini outputs.
2. `spatial.create_spatial_map` — ask Gemini for a single 10×10 grid placement list; if Gemini is unavailable/empty, `_heuristic_grid_map` rescales to the [0,9] range.
3. `render_map.placements_from_grid_centers` expands each cell into a rectangle using `grid_footprint_cells` name heuristics.

**Rendering & Braille:** `render_map.rasterize` paints rectangles lowest-confidence-first so strong detections occlude weak ones. `render_map.symbol_for_name` picks distinct glyphs (sofa=█, bed=▓, table=▬, chair=◆, …) with unique-per-placement fallback. `braille._TACTILE` then maps each glyph to a **distinct** Braille cell so different furniture feels different under touch.

**External calls:** all Gemini traffic flows through `gemini_client._generate`, which wraps `google.genai` in a `ThreadPoolExecutor` for a hard timeout (`_VISION_TIMEOUT_S=90`, text 2048 tokens / image 16384). Any failure/timeout returns `""` and callers degrade gracefully. `GEMINI_API_KEY` is loaded by `_load_env` from `<repo>/.env` first, then CWD `.env`.

## Things to know before editing

- `braille_local/` is a flat package with no `__init__.py` — imports are bare (`from detect import ...`). Run from inside that directory or adjust `sys.path`; don't reorganize into subpackages without updating every import.
- YOLO is lazy-singleton (`detect._yolo_model`) — the first call loads `yolov8n.pt` from CWD. Keep the weights file at the repo root or run from there.
- Coordinate convention: the **architecture** pipeline carries `norm_*` (0–1) end-to-end; the **legacy** pipeline uses pixel `x,y` and only adds `norm_*` at merge time via `render_map.enrich_objects_with_norm_bbox`. Don't mix them.
- When adding a new furniture class, touch three places: `render_map._NAME_TO_SYMBOL` (glyph), `render_map.grid_footprint_cells` (legacy footprint), and optionally `layout._BUCKET_MAX` (cap).
- Braille glyph assignments in `braille._TACTILE` are intentionally distinct per shape symbol; if you add a new symbol in `render_map`, add a matching Braille cell here or it falls back to `FILLED` and loses tactile distinctiveness.
