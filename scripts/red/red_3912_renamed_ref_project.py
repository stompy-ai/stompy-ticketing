"""RED for STOMPY-3912: restore the string comparison; the named tests fail at pytest exit 1.

Runs in a throwaway copy of the tree; the working tree is never edited.
"""

import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
T = "tests/test_3912_renamed_ref_project.py::"
ALL = {T + n for n in (
    "test_ref_with_the_current_name_of_its_renamed_project_is_accepted",
    "test_batch_refs_with_the_current_name_are_accepted",
    "test_a_really_different_project_is_still_refused",
    "test_an_unresolvable_project_is_still_refused",
    "test_link_between_refs_of_the_renamed_project_is_not_a_cross_project_link",
)}
CUT = (
    "stompy_ticketing/mcp_tools.py",
    "            return resolve_schema_func(project) == ref_project\n",
    "            return False\n",
)
EXPECTED = {T + "test_ref_with_the_current_name_of_its_renamed_project_is_accepted",
            T + "test_batch_refs_with_the_current_name_are_accepted",
            T + "test_link_between_refs_of_the_renamed_project_is_not_a_cross_project_link"}


def run(tree):
    xml = Path(tree) / "junit.xml"
    code = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           T.rstrip(":"), f"--junitxml={xml}"], cwd=tree).returncode
    failed = set()
    for case in ET.parse(xml).iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            failed.add("tests/" + case.get("classname").split(".")[-1] + ".py::" + case.get("name"))
    return code, failed


def main():
    with tempfile.TemporaryDirectory(prefix="red-3912-") as tmp:
        tree = Path(tmp) / "tree"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", ".worktrees", ".claude"))
        code, failed = run(tree)
        assert code == 0 and not failed, f"GREEN expected, got exit {code}: {failed}"
        path, before, after = CUT
        src = (tree / path).read_text()
        assert src.count(before) == 1, "cut anchor moved"
        (tree / path).write_text(src.replace(before, after))
        code, failed = run(tree)
        print({"pytest_exit": code, "failed": sorted(failed)})
        assert code == 1 and failed == EXPECTED, f"want exit 1 failing {sorted(EXPECTED)}"
        assert ALL - EXPECTED, "controls must exist"
    print("PASS: named cut fails exactly its tests; controls green")


if __name__ == "__main__":
    main()
