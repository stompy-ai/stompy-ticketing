"""Behavioral RED proof for STOMPY-462; requires disposable STOMPY_462_LOCAL_PG.

Every mutation preserves imports/signatures and must fail the exact declared
names at pytest exit 1; all named controls must pass. Runs only in copies.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NATIVE = "tests/test_ticket_grep_462.py::"
UNIT = "tests/test_ticket_grep_contract_462.py::"
FILES = [
    "tests/test_ticket_grep_462.py",
    "tests/test_ticket_grep_contract_462.py",
    "tests/test_cluster_c_fixes.py::TestBug180GrepAggregates",
]
CONTROLS = {
    NATIVE + "test_no_grep_keeps_original_page",
    NATIVE + "test_native_glob_matches_fnmatch[*]",
    UNIT + "test_default_filter_leaves_glob_unset",
    "tests/test_cluster_c_fixes.py::TestBug180GrepAggregates::test_no_grep_keeps_original_aggregates",
}
INITIAL_RED = {
    NATIVE + "test_native_mcp_invalid_pattern_is_a_client_refusal",
    NATIVE + "test_offset_past_matching_set_keeps_its_total",
    "tests/test_ticket_grep_462.py::test_native_glob_matches_fnmatch[[[]*]",
    "tests/test_ticket_grep_462.py::test_native_glob_matches_fnmatch[[!A]*]",
    "tests/test_ticket_grep_462.py::test_mcp_preserves_whole_filtered_totals_on_a_small_page",
    "tests/test_ticket_grep_462.py::test_native_glob_matches_fnmatch[[a-c]*]",
    "tests/test_ticket_grep_462.py::test_native_glob_matches_fnmatch[100%_*]",
    "tests/test_ticket_grep_462.py::test_native_glob_matches_fnmatch[caf\\xe9]",
    "tests/test_ticket_grep_462.py::test_native_glob_matches_fnmatch[a*]",
    "tests/test_ticket_grep_462.py::test_native_glob_matches_fnmatch[a\\\\*]",
    "tests/test_ticket_grep_462.py::test_native_glob_matches_fnmatch[a?]",
    "tests/test_ticket_grep_462.py::test_native_glob_matches_fnmatch[*a*b*]",
    "tests/test_ticket_grep_462.py::test_no_match_is_empty_without_more",
    "tests/test_ticket_grep_462.py::test_native_glob_matches_fnmatch[bracket[]",
    "tests/test_ticket_grep_462.py::test_native_glob_matches_fnmatch[a'b]",
    "tests/test_ticket_grep_462.py::test_matching_page_and_aggregates_precede_pagination",
    "tests/test_ticket_grep_462.py::test_native_glob_matches_fnmatch[[c-a]*]",
    "tests/test_ticket_grep_462.py::test_native_glob_matches_fnmatch[[!]*]",
    "tests/test_ticket_grep_462.py::test_native_glob_matches_fnmatch[*\\n*]",
}
REVERTS = [
    (
        "ignore_title_predicate",
        "stompy_ticketing/ticket_listing.py",
        "if filters.grep:",
        "if False and filters.grep:",
        INITIAL_RED | {UNIT + "test_all_list_queries_bind_the_same_title_predicate"},
    ),
    (
        "omit_mcp_filter",
        "stompy_ticketing/mcp_tools.py",
        "grep=grep,",
        "grep=None,",
        {
            NATIVE + "test_native_mcp_invalid_pattern_is_a_client_refusal",
            NATIVE + "test_mcp_preserves_whole_filtered_totals_on_a_small_page",
            UNIT + "test_mcp_forwards_glob_and_retains_out_of_page_matches",
            "tests/test_cluster_c_fixes.py::TestBug180GrepAggregates::test_grep_filter_recomputes_by_status",
        },
    ),
    (
        "unanchor_glob",
        "stompy_ticketing/ticket_listing.py",
        'return r"(?s)\\A" +',
        'return r"(?s)" +',
        {
            NATIVE + "test_native_glob_matches_fnmatch[" + pattern + "]"
            for pattern in ("a*", "a?", "[!A]*", "[a-c]*", "[[]*")
        }
        | {
            NATIVE + "test_native_bracket_classes_match_oracle",
            UNIT + "test_translation_is_independent_of_cpython_regex_generation",
        },
    ),
    (
        "recount_the_page",
        "stompy_ticketing/mcp_tools.py",
        '                    return _safe_json(result)\n\n                elif action == "list_tags":',
        "                    result.total = len(result.tickets)\n"
        '                    return _safe_json(result)\n\n                elif action == "list_tags":',
        {
            NATIVE + "test_mcp_preserves_whole_filtered_totals_on_a_small_page",
            UNIT + "test_mcp_forwards_glob_and_retains_out_of_page_matches",
            "tests/test_cluster_c_fixes.py::TestBug180GrepAggregates::test_grep_filter_recomputes_by_status",
        },
    ),
    (
        "do_not_classify_pattern_rejection",
        "stompy_ticketing/service.py",
        "        except InvalidRegularExpression as exc:\n",
        "        except InvalidRegularExpression as exc:\n            raise\n",
        {
            UNIT + "test_postgres_pattern_rejection_is_a_client_refusal",
            NATIVE + "test_native_mcp_invalid_pattern_is_a_client_refusal",
        },
    ),
]


def run(copy):
    env = {**os.environ, "PYTHONPATH": str(copy)}
    imported = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import stompy_ticketing; print(stompy_ticketing.__file__)",
        ],
        cwd=copy,
        env=env,
        text=True,
    ).strip()
    assert Path(imported).resolve().is_relative_to(copy.resolve()), imported
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *FILES,
            "-q",
            "-p",
            "no:cacheprovider",
            "--junitxml=outcomes.xml",
        ],
        cwd=copy,
        env=env,
        capture_output=True,
        text=True,
    )
    outcomes = {}
    for case in ET.parse(copy / "outcomes.xml").iter("testcase"):
        classname = case.attrib["classname"]
        if classname.endswith(".TestBug180GrepAggregates"):
            name = (
                "tests/test_cluster_c_fixes.py::TestBug180GrepAggregates::"
                + case.attrib["name"]
            )
        else:
            name = classname.replace(".", "/") + ".py::" + case.attrib["name"]
        assert name not in outcomes, name
        outcomes[name] = (
            "error"
            if case.find("error") is not None
            else (
                "failed"
                if case.find("failure") is not None
                else "skipped" if case.find("skipped") is not None else "passed"
            )
        )
    return result.returncode, outcomes, result.stdout + result.stderr


def main():
    assert os.environ.get("STOMPY_462_LOCAL_PG"), "disposable local PG is required"
    report = {}
    with tempfile.TemporaryDirectory(prefix="astra-462-red-") as tmp:
        for name, file, needle, broken, expected in [
            ("green", None, None, None, set()),
            *REVERTS,
        ]:
            copy = Path(tmp) / name
            shutil.copytree(
                ROOT,
                copy,
                ignore=shutil.ignore_patterns(
                    ".git", ".venv*", "__pycache__", "*.egg-info", ".pytest_cache"
                ),
            )
            if file:
                path = copy / file
                text = path.read_text()
                assert text.count(needle) == 1, (name, text.count(needle))
                path.write_text(text.replace(needle, broken))
            code, outcomes, output = run(copy)
            failed = {key for key, verdict in outcomes.items() if verdict != "passed"}
            report[name] = {"exit": code, "failed": sorted(failed)}
            print(name, json.dumps(report[name]), flush=True)
            assert code == (1 if expected else 0), output
            assert failed == expected, (failed ^ expected, output)
            assert all(outcomes.get(control) == "passed" for control in CONTROLS)
    print("PASS: exact named failure sets, pytest exits and controls verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
