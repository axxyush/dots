"""Launch all four BrailleMap agents as child processes with prefixed logs.

    python run_all.py

Ctrl-C terminates all children cleanly. If any child exits unexpectedly,
`run_all` tears the rest down so you don't end up with a partial pipeline.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import List, Tuple

from dotenv import load_dotenv

from schemas import address_from_seed

load_dotenv()

AGENTS: List[Tuple[str, str, str, str]] = [
    # (label, script, color, seed_env_var)
    ("spatial  ", "agent_spatial.py",         "\033[36m", "AGENT_SEED_1"),  # cyan
    ("enricher ", "agent_enricher.py",        "\033[33m", "AGENT_SEED_2"),  # yellow
    ("map      ", "agent_map.py",             "\033[35m", "AGENT_SEED_3"),  # magenta
    ("narration", "agent_narration.py",       "\033[32m", "AGENT_SEED_4"),  # green
    ("floorplan", "agent_floorplan.py",       "\033[34m", "AGENT_SEED_5"),  # blue
    ("ada-rec  ", "agent_recommendations.py", "\033[91m", "AGENT_SEED_6"),  # red
]
RESET = "\033[0m"


def _stream(label: str, color: str, proc: subprocess.Popen) -> None:
    prefix = f"{color}[{label}]{RESET} "
    assert proc.stdout is not None
    for line in iter(proc.stdout.readline, b""):
        sys.stdout.write(prefix + line.decode(errors="replace"))
        sys.stdout.flush()


def _print_expected_addresses() -> None:
    print("Expected agent addresses (deterministic from AGENT_SEED_*):")
    for label, _script, color, seed_var in AGENTS:
        seed = os.getenv(seed_var)
        if not seed:
            print(f"  {color}{label}{RESET}  (seed {seed_var} not set!)")
            continue
        try:
            addr = address_from_seed(seed)
        except Exception as e:
            addr = f"(error computing: {e})"
        print(f"  {color}{label}{RESET}  {addr}")
    print()


def main() -> None:
    _print_expected_addresses()

    procs: List[Tuple[str, subprocess.Popen]] = []
    here = os.path.dirname(os.path.abspath(__file__))

    for label, script, color, _seed_var in AGENTS:
        script_path = os.path.join(here, script)
        proc = subprocess.Popen(
            [sys.executable, "-u", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=here,
        )
        procs.append((label, proc))
        threading.Thread(target=_stream, args=(label, color, proc), daemon=True).start()
        # Small stagger so ports bind cleanly and log lines don't interleave at boot.
        time.sleep(0.6)

    def _shutdown(*_: object) -> None:
        print("\n[run_all] shutting down…")
        for _label, proc in procs:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        deadline = time.time() + 5
        for _label, proc in procs:
            remaining = max(0.0, deadline - time.time())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # If any child dies on its own, shut the rest down too.
    while True:
        for label, proc in procs:
            rc = proc.poll()
            if rc is not None:
                print(f"[run_all] agent '{label}' exited with code {rc} — tearing down")
                _shutdown()
        time.sleep(1)


if __name__ == "__main__":
    main()
