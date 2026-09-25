"""RED evidence for STOMPY-2455: a create names the tickets it may duplicate.

Each revert is NAMED and BEHAVIOURAL (it neuters one decision, never an
import), is applied to a throwaway copy of this checkout, and must:
  - find its needle EXACTLY ONCE (a drifted needle would silently no-op),
  - make pytest exit EXACTLY 1 (2-5 are collection/usage errors, not RED),
  - fail EXACTLY the named tests — not "some number of" them,
  - leave every control GREEN.
The unreverted copy must then be fully GREEN. Exit 0 only if all of that holds.

    python3 scripts/red/red_2455_duplicate_hints.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = ["tests/test_duplicate_hints_2455.py", "tests/test_ticket_authors_1594.py"]
T = "tests/test_duplicate_hints_2455.py::"
CONTROLS = {
    T + "test_content_hash_is_the_one_formula_create_writes",
    T + "test_a_hint_carries_display_id_title_status_and_a_rounded_similarity",
    T + "test_dry_run_is_refused_outside_create",
    # The MCP create's own actor contract — this PR must not move it.
    "tests/test_ticket_authors_1594.py::TestMcpWritesCarryTheActor::"
    "test_create_and_every_transition_write_the_actor",
}
DUP = "stompy_ticketing/duplicates.py"
MCP = "stompy_ticketing/mcp_tools.py"
REVERTS = [
    ("create_response_drops_the_hints", MCP,
     '"ticket": result.model_dump(),\n                        **duplicates.response_fields(hints),\n',
     '"ticket": result.model_dump(),\n',
     {T + "test_mcp_create_returns_the_hints_and_the_guidance",
      T + "test_mcp_create_survives_a_failed_check_and_says_so"}),
    ("create_hints_match_the_new_ticket_itself", MCP,
     "exclude_id=result.id, prefix=prefix", "exclude_id=None, prefix=prefix",
     {T + "test_mcp_create_returns_the_hints_and_the_guidance"}),
    ("dry_run_creates_anyway", MCP,
     "                    if dry_run:\n", "                    if False:\n",
     {T + "test_mcp_create_dry_run_returns_candidates_and_writes_nothing"}),
    ("no_threshold", DUP,
     'if float(r["similarity"]) >= SIMILARITY_THRESHOLD', "if True",
     {T + "test_hints_below_the_threshold_are_dropped",
      T + "test_nothing_similar_is_an_empty_list_not_a_failure"}),
    ("no_cap", DUP, "for r in hints[:MAX_HINTS]", "for r in hints",
     {T + "test_hints_are_capped_best_first"}),
    ("a_failed_check_fails_the_create", DUP,
     "    except Exception as e:\n        try:\n",
     "    except Exception as e:\n        raise\n        try:\n",
     {T + "test_a_failing_check_returns_none_rolls_back_and_names_a_warning",
      T + "test_a_failing_rollback_is_still_soft",
      T + "test_an_unreadable_row_is_still_soft"}),
    ("a_failed_check_reads_as_no_duplicates", DUP,
     '    if hints is None:\n        return {"duplicate_check": "unavailable"}',
     '    if hints is None:\n        return {"possible_duplicates": []}',
     {T + "test_response_fields_name_an_unavailable_check_instead_of_silence",
      T + "test_mcp_create_survives_a_failed_check_and_says_so"}),
    ("content_hash_stays_unread", DUP,
     "coalesce(c.content_hash = %(hash)s, false) AS exact", "false AS exact",
     {T + "test_the_query_reads_content_hash_as_an_exact_match"}),
    ("closed_tickets_are_candidates", DUP,
     " AND NOT (status = ANY(%(terminal)s))", "",
     {T + "test_the_query_is_bounded_and_scoped_to_open_tickets"}),
    ("unbounded_candidate_scan", DUP,
     "        LIMIT %(max_candidates)s\n", "",
     {T + "test_the_query_is_bounded_and_scoped_to_open_tickets"}),
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
    with tempfile.TemporaryDirectory(prefix="red-2455-") as tmp:
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
