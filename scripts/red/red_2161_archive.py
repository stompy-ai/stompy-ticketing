"""Run plugin archive decisions through the companion host's real PG doors."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __name__ == "__main__":
    host = Path(os.environ["STOMPY_HOST_SOURCE"]).resolve()
    script = host / "scripts/red/red_2161_ticket_archive.py"
    assert script.is_file(), "backend companion with named2161 proof is required"
    sys.path.insert(0, str(host / "scripts/red"))
    from behavioral_reverts import run_reverts

    root = Path(__file__).resolve().parents[2]
    prefix = "tests.test_archive_core_2161::"
    controls = {prefix + name for name in (
        "test_live_nonterminal_archive_is_named_refusal_and_does_not_write",
        "test_unarchive_preserves_status_and_clears_only_archival_marker",
        "test_batch_archive_preview_does_not_commit_or_update",
    )}
    event_reverts = [
        ("lose_project_event_name", "stompy_ticketing/archival.py",
         '"ticket_unarchived" if restoring else "ticket_archived",\n            ticket=ticket_id,\n            project=project,',
         '"ticket_unarchived" if restoring else "ticket_archived",\n            ticket=ticket_id,\n            project=schema,',
         {prefix + "test_archive_event_preserves_project_name_not_database_schema"}),
        ("lose_invalid_ids_reason", "stompy_ticketing/archival.py",
         'reason="invalid_ids",', 'reason="invalid_ids_ignored",',
         {prefix + "test_invalid_batch_ids_emit_named_refusal_without_sql"}),
    ]
    # The shared host harness provisions a package-only admin import. Compose
    # that real package too, so its symlink is valid on every repeated run.
    with tempfile.TemporaryDirectory(prefix="astra-2161-plugin-proof-") as directory:
        composed = Path(directory) / "source"
        shutil.copytree(root, composed, ignore=shutil.ignore_patterns(
            ".git", ".worktrees", ".venv*", "__pycache__", ".env*", "build", "*.egg-info"))
        shutil.copytree(host / "stompy-admin/stompy_admin", composed / "stompy-admin/stompy_admin",
                        ignore=shutil.ignore_patterns("__pycache__"))
        run_reverts(composed, ["tests/test_archive_core_2161.py"], controls, event_reverts,
                    prefix="astra-2161-events-", extra_env={"PYTHONPATH": "."})
    env = {**os.environ, "STOMPY_TICKETING_SOURCE": str(Path(__file__).resolve().parents[2])}
    raise SystemExit(subprocess.call([sys.executable, str(script)], cwd=host, env=env))
