"""Reproduce codec decisions in isolated copies; exact failures, GREEN controls."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = "stompy_ticketing/response_encoding.py"
TEST = "tests/test_roundtrip_3757.py"
PREFIX = "tests.test_roundtrip_3757::"
CONTROLS = {
    PREFIX + name
    for name in (
        "test_recovery_values_survive_real_encoder[comma-quote]",
        "test_recovery_values_survive_real_encoder[newline]",
        "test_encoder_exception_control_uses_json",
        "test_ordinary_toon_control_stays_compact",
        "test_registered_link_control_preserves_colon_labels",
        "test_rest_control_returns_the_same_values_without_toon",
    )
}
LOSSY = {
    PREFIX + f"test_decodable_but_changed_output_uses_json[bad{i}]" for i in (0, 1)
}
INVALID = {
    PREFIX + "test_recovery_values_survive_real_encoder[colon]",
    PREFIX + "test_recovery_values_survive_real_encoder[unicode-colon]",
    PREFIX + "test_accepted_ticket_tags_survive_real_encoder",
    *(
        PREFIX + f"test_registered_mcp_ticket_shapes_keep_tags_and_recovery[{door}]"
        for door in ("get", "list", "search", "board")
    ),
}


def verdicts(report):
    result = {"failed": set(), "passed": set(), "other": set()}
    for test in ET.parse(report).iter("testcase"):
        name = test.attrib["classname"] + "::" + test.attrib["name"]
        kind = (
            "failed"
            if test.find("failure") is not None
            else (
                "other"
                if test.find("error") is not None or test.find("skipped") is not None
                else "passed"
            )
        )
        assert name not in set().union(*result.values()), name
        result[kind].add(name)
    return result


def run(repo, report, expected, universe=None):
    env = {**os.environ, "PYTHONPATH": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            TEST,
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    actual = verdicts(report)
    assert result.returncode == (1 if expected else 0), result.stdout + result.stderr
    assert actual["failed"] == expected and not actual["other"], (actual, result.stdout)
    assert CONTROLS <= actual["passed"], actual
    names = actual["failed"] | actual["passed"]
    assert names and (universe is None or names == universe), actual
    print(
        json.dumps(
            {
                "pytest_exit": result.returncode,
                "failed": sorted(expected),
                "controls": sorted(CONTROLS),
                "total": len(names),
            }
        ),
        flush=True,
    )
    return names


def main():
    original = (ROOT / TARGET).read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    before = "        if decode(encoded) == data:\n            return encoded"
    reverts = (
        (
            "accept_undecodable_and_lossy",
            "        if True:\n            return encoded",
            INVALID | LOSSY,
        ),
        (
            "accept_decodable_but_lossy",
            "        if decode(encoded) is not None:\n            return encoded",
            LOSSY
            | {
                PREFIX
                + f"test_registered_mcp_ticket_shapes_keep_tags_and_recovery[{door}]"
                for door in ("list", "search", "board")
            },
        ),
    )
    with tempfile.TemporaryDirectory(prefix="astra-3757-red-") as directory:
        repo = Path(directory) / "repo"
        shutil.copytree(
            ROOT,
            repo,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv*",
                "__pycache__",
                ".pytest_cache",
                "*.egg-info",
                "build",
                "dist",
            ),
        )
        universe = run(repo, Path(directory) / "green.xml", set())
        for name, after, expected in reverts:
            source = original.decode()
            assert source.count(before) == 1, "semantic decision needle drifted"
            (repo / TARGET).write_text(source.replace(before, after, 1))
            print(name, flush=True)
            run(repo, Path(directory) / f"{name}.xml", expected, universe)
            (repo / TARGET).write_bytes(original)
    assert hashlib.sha256((ROOT / TARGET).read_bytes()).hexdigest() == digest
    print("PASS: exact RED NAME sets, controls, identity universe and source readback")


if __name__ == "__main__":
    main()
