"""Run this plugin's lease decisions through the companion host's real PG doors.

Set STOMPY_HOST_SOURCE to the backend companion checkout, TEST_DATABASE_URL
to isolated PostgreSQL, and STOMPY_CLI_BIN to the built lease CLI.
"""

import os
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    host = Path(os.environ["STOMPY_HOST_SOURCE"]).resolve()
    script = host / "scripts/red/red_2157_ticket_leases.py"
    assert script.is_file(), "backend companion with named2157 proof is required"
    env = {**os.environ, "STOMPY_TICKETING_SOURCE": str(Path(__file__).resolve().parents[2])}
    raise SystemExit(subprocess.call([sys.executable, str(script)], cwd=host, env=env))
