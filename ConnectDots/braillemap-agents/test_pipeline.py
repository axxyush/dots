"""End-to-end pipeline test harness.

    python test_pipeline.py <room_id>
    python test_pipeline.py --via-backend <room_id>   # hit POST /trigger instead

Sends a SpatialProcessingRequest to Agent 1 (or asks the backend to do it),
then polls GET /rooms/{id} every 3s and prints which fields have been
populated by each downstream agent. Exits when layout_2d, pdf_url, and
audio_url are all present, or after the overall timeout.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Dict

import requests
from dotenv import load_dotenv

load_dotenv()

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
POLL_INTERVAL = 3
OVERALL_TIMEOUT = 300  # 5 minutes

DESIRED_FIELDS = ["layout_2d", "pdf_url", "audio_url", "narration_text"]


def fetch_room(room_id: str) -> Dict[str, Any]:
    resp = requests.get(f"{BACKEND_URL}/rooms/{room_id}", timeout=15)
    resp.raise_for_status()
    return resp.json()


def trigger_via_backend(room_id: str) -> None:
    resp = requests.post(f"{BACKEND_URL}/trigger/{room_id}", timeout=15)
    resp.raise_for_status()
    print(f"Backend accepted trigger: {resp.json()}")


def trigger_directly(room_id: str) -> None:
    # Import lazily so the script still runs if uagents isn't installed for other flows.
    from trigger import trigger_spatial_pipeline
    trigger_spatial_pipeline(room_id)
    print(f"Sent SpatialProcessingRequest directly to Agent 1 for room {room_id}")


def summarize(room: Dict[str, Any]) -> str:
    parts = []
    status = room.get("status")
    if status:
        parts.append(f"status={status}")
    for field in DESIRED_FIELDS:
        val = room.get(field)
        if val:
            if isinstance(val, str):
                preview = val if len(val) < 60 else val[:57] + "…"
                parts.append(f"{field}={preview!r}")
            elif isinstance(val, dict):
                parts.append(f"{field}=✓(dict, {len(val)} keys)")
            else:
                parts.append(f"{field}=✓")
        else:
            parts.append(f"{field}=—")
    return "  ".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("room_id", nargs="?", help="room id (prompt if omitted)")
    ap.add_argument("--via-backend", action="store_true",
                    help="trigger via POST /trigger/{id} instead of direct message")
    args = ap.parse_args()

    room_id = args.room_id or input("room_id: ").strip()
    if not room_id:
        print("no room_id supplied", file=sys.stderr)
        sys.exit(1)

    print(f"Backend   : {BACKEND_URL}")
    print(f"Room ID   : {room_id}")
    print(f"Mode      : {'via backend /trigger' if args.via_backend else 'direct to Agent 1'}")
    print()

    # Confirm the room exists before triggering.
    try:
        fetch_room(room_id)
    except Exception as e:
        print(f"Cannot reach room {room_id}: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.via_backend:
            trigger_via_backend(room_id)
        else:
            trigger_directly(room_id)
    except Exception as e:
        print(f"Failed to trigger pipeline: {e}", file=sys.stderr)
        sys.exit(1)

    print("\nPolling room state (Ctrl-C to stop)…\n")
    start = time.time()
    last_summary = None
    while time.time() - start < OVERALL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        try:
            room = fetch_room(room_id)
        except Exception as e:
            print(f"  poll error: {e}")
            continue
        summary = summarize(room)
        if summary != last_summary:
            elapsed = int(time.time() - start)
            print(f"[{elapsed:>4}s] {summary}")
            last_summary = summary
        if all(room.get(f) for f in DESIRED_FIELDS):
            print()
            print("✓ Pipeline complete.")
            print(f"  PDF  : {room.get('pdf_url')}")
            print(f"  MP3  : {room.get('audio_url')}")
            narration = room.get("narration_text") or ""
            print(f"  Text : {narration[:200]}…" if len(narration) > 200 else f"  Text : {narration}")
            return

    print("\n⚠ Timed out before all outputs landed.")
    sys.exit(2)


if __name__ == "__main__":
    main()
