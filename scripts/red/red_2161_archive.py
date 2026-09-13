"""Run plugin archive decisions through the companion host's real PG doors."""

import os
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    host = Path(os.environ["STOMPY_HOST_SOURCE"]).resolve()
    script = host / "scripts/red/red_2161_ticket_archive.py"
    assert script.is_file(), "backend companion with named2161 proof is required"
    env = {**os.environ, "STOMPY_TICKETING_SOURCE": str(Path(__file__).resolve().parents[2])}
    raise SystemExit(subprocess.call([sys.executable, str(script)], cwd=host, env=env))
