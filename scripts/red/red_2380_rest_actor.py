"""RED evidence for STOMPY-2380: a REST write records the caller, never a note.

Each revert is NAMED and BEHAVIOURAL (it neuters one decision, never an
import), is applied to a throwaway copy of this checkout, and must:
  - find its needle EXACTLY ONCE (a drifted needle would silently no-op),
  - make pytest exit EXACTLY 1 (2-5 are collection/usage errors, not RED),
  - fail EXACTLY the named tests — not "some number of" them,
  - leave every control GREEN.
The unreverted copy must then be fully GREEN. Exit 0 only if all of that holds.

    python3 scripts/red/red_2380_rest_actor.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = ["tests/test_rest_actor_2380.py", "tests/test_ticket_authors_1594.py"]
T = "tests/test_rest_actor_2380.py::"
CONTROLS = {
    T + "test_control_batch_move_still_forwards_its_other_fields",
    T + "test_control_single_move_still_forwards_park_reason",
    # The MCP door's own actor contract — this PR must not move it.
    "tests/test_ticket_authors_1594.py::TestMcpWritesCarryTheActor::"
    "test_create_and_every_transition_write_the_actor",
}
ROUTES = "stompy_ticketing/api_routes.py"
REVERTS = [
    ("batch_move_note_is_the_actor", ROUTES,
     "            changed_by=_actor(),\n            reason=body.reason,",
     "            changed_by=body.note,\n            reason=body.reason,",
     {T + "test_batch_move_note_naming_another_user_is_not_the_actor[51]",
      T + "test_batch_move_note_naming_another_user_is_not_the_actor[6746]",
      T + "test_without_an_actor_hook_a_note_still_never_becomes_the_actor",
      T + "test_register_plugin_hands_the_actor_hook_to_the_rest_door"}),
    ("batch_close_note_is_the_actor", ROUTES,
     "            confirm=body.confirm,\n            changed_by=_actor(),\n        )",
     "            confirm=body.confirm,\n            changed_by=body.note,\n        )",
     {T + "test_batch_close_note_naming_another_user_is_not_the_actor[51]",
      T + "test_batch_close_note_naming_another_user_is_not_the_actor[6746]",
      T + "test_without_an_actor_hook_a_note_still_never_becomes_the_actor"}),
    ("put_records_nobody", ROUTES,
     "ticket_id, body, changed_by=_actor())", "ticket_id, body)",
     {T + "test_put_update_records_the_caller"}),
    ("single_move_records_nobody", ROUTES,
     "                changed_by=_actor(),\n            )", "            )",
     {T + "test_single_move_records_the_caller"}),
    ("registration_keeps_actor_off_rest", "stompy_ticketing/plugin.py",
     "        actor_func=actor_func,  # 2380: REST stamps the same identity as MCP\n", "",
     {T + "test_register_plugin_hands_the_actor_hook_to_the_rest_door"}),
    ("raising_hook_fails_the_write", ROUTES,
     "    try:\n        return _actor_func() if _actor_func else None\n"
     "    except Exception:\n        return None",
     "    return _actor_func() if _actor_func else None",
     {T + "test_a_raising_actor_hook_never_fails_the_write"}),
]
_LINE = re.compile(r"^(PASSED|FAILED|ERROR) (\S+)")


def run(copy: Path):
    env = {**os.environ, "PYTHONPATH": str(copy)}
    where = subprocess.run(
        [sys.executable, "-c", "import stompy_ticketing; print(stompy_ticketing.__file__)"],
        cwd=copy, env=env, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert Path(where).resolve().is_relative_to(copy.resolve()), f"wrong package: {where}"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *FILES, "-q", "-p", "no:cacheprovider", "-rA"],
        cwd=copy, env=env, capture_output=True, text=True,
    )
    outcomes = {}
    for line in proc.stdout.splitlines():
        m = _LINE.match(line)
        if m:
            outcomes[m.group(2)] = m.group(1)
    return proc.returncode, outcomes


def main() -> int:
    problems = []
    with tempfile.TemporaryDirectory(prefix="red-2380-") as tmp:
        for name, rel, needle, broken, expected in REVERTS:
            copy = Path(tmp) / name
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(
                ".git", ".worktrees", ".venv*", "__pycache__", "build", "*.egg-info"))
            target = copy / rel
            text = target.read_text()
            if text.count(needle) != 1:
                problems.append(f"{name}: needle found {text.count(needle)}x in {rel}, not once")
                continue
            target.write_text(text.replace(needle, broken))
            code, outcomes = run(copy)
            failed = {k for k, v in outcomes.items() if v != "PASSED"}
            print(f"===== REVERT {name}: pytest exit {code}")
            for test in sorted(failed):
                print(f"  {outcomes[test]} {test}")
            if code != 1:
                problems.append(f"{name}: pytest exit {code}, not 1")
            if failed != expected:
                problems.append(f"{name}: failed {sorted(failed)} != expected {sorted(expected)}")
            dead = {c for c in CONTROLS if outcomes.get(c) != "PASSED"}
            if dead:
                problems.append(f"{name}: control(s) not GREEN: {sorted(dead)}")
        copy = Path(tmp) / "restored"
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(
            ".git", ".worktrees", ".venv*", "__pycache__", "build", "*.egg-info"))
        code, outcomes = run(copy)
        passed = sum(v == "PASSED" for v in outcomes.values())
        print(f"===== RESTORED: pytest exit {code}, {passed}/{len(outcomes)} passed")
        if code != 0 or passed != len(outcomes) or not CONTROLS <= outcomes.keys():
            problems.append("restored tree is not GREEN")
    for p in problems:
        print("!!", p)
    print("NOT EVIDENCE" if problems else "all reverts RED by name, controls GREEN, restored GREEN")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
