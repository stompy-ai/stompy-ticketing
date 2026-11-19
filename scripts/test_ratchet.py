"""Fixture probes for scripts/ratchet.py (STOMPY-2811). Offline: jscpd is never run."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ratchet  # noqa: E402

CFG = {
    "extensions": [".ts"],
    "ignore_dirs": ["node_modules"],
    "exclude": [
        {"glob": "*.test.ts", "class": "test", "reason": "tests"},
        {"glob": "gen/*", "class": "generated", "reason": "generated"},
    ],
    "clone": {"format": "typescript"},
}


def tree(files: dict, baseline=None) -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "ratchet_config.json").write_text(json.dumps(CFG))
    for rel, n in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n" * n)
    if baseline is not None:
        (root / ratchet.SIZE_BASELINE).write_text(json.dumps(baseline))
    return root


def run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HERE / "ratchet.py"), *args, "--root", str(root)],
                          capture_output=True, text=True)


class SizeRatchet(unittest.TestCase):
    def test_new_501_line_file_fails(self):
        r = run(tree({"a.ts": 501}, {}), "size")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NEW: a.ts", r.stdout)

    def test_exactly_500_lines_passes(self):
        r = run(tree({"a.ts": 500}, {}), "size")
        self.assertEqual(r.returncode, 0)

    def test_growth_of_grandfathered_file_fails(self):
        root = tree({"big.ts": 701}, {"big.ts": 700})
        r = run(root, "size")
        self.assertEqual(r.returncode, 1)
        self.assertIn("RATCHET: big.ts grew 700 -> 701", r.stdout)

    def test_unchanged_legacy_debt_passes(self):
        root = tree({"big.ts": 700}, {"big.ts": 700})
        r = run(root, "size")
        self.assertEqual(r.returncode, 0)

    def test_unrecorded_shrink_is_stale(self):
        root = tree({"big.ts": 650}, {"big.ts": 700})
        r = run(root, "size")
        self.assertEqual(r.returncode, 1)
        self.assertIn("STALE: big.ts", r.stdout)

    def test_deleted_file_is_stale(self):
        root = tree({}, {"gone.ts": 700})
        r = run(root, "size")
        self.assertEqual(r.returncode, 1)
        self.assertIn("STALE: gone.ts", r.stdout)

    def test_classified_test_and_generated_are_exempt(self):
        root = tree({"a.test.ts": 900, "gen/x.ts": 900, "node_modules/p/i.ts": 900}, {})
        r = run(root, "size")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_nested_production_module_is_seen(self):
        root = tree({"app/deep/er/page.ts": 600}, {})
        r = run(root, "size")
        self.assertEqual(r.returncode, 1)
        self.assertIn("app/deep/er/page.ts", r.stdout)

    def test_write_baseline_refuses_growth_and_accepts_shrink(self):
        root = tree({"big.ts": 701}, {"big.ts": 700})
        r = run(root, "size", "--write-baseline")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads((root / ratchet.SIZE_BASELINE).read_text()), {"big.ts": 700})
        root = tree({"big.ts": 650}, {"big.ts": 700})
        r = run(root, "size", "--write-baseline")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads((root / ratchet.SIZE_BASELINE).read_text()), {"big.ts": 650})

    def test_exclude_without_reason_is_rejected(self):
        root = tree({})
        bad = dict(CFG, exclude=[{"glob": "*.ts", "class": "test"}])
        (root / "ratchet_config.json").write_text(json.dumps(bad))
        with self.assertRaises(ValueError):
            ratchet.load_config(root)


def dup(a, b, frag, lines=10):
    return {"firstFile": {"name": a}, "secondFile": {"name": b}, "fragment": frag, "lines": lines, "tokens": 80}


class CloneRatchet(unittest.TestCase):
    def test_new_clone_fails(self):
        cur = ratchet.summarize([dup("a.ts", "b.ts", "x = 1")])
        f = ratchet.clone_failures(cur, {})
        self.assertTrue(f and f[0].startswith("NEW: a.ts <-> b.ts"))

    def test_increased_repetitions_fail(self):
        base = ratchet.summarize([dup("a.ts", "b.ts", "x = 1")])
        cur = ratchet.summarize([dup("a.ts", "b.ts", "x = 1"), dup("b.ts", "a.ts", "  x = 1\n")])
        f = ratchet.clone_failures(cur, base)
        self.assertEqual(len(f), 1)
        self.assertTrue(f[0].startswith("RATCHET:"))

    def test_unchanged_legacy_clone_passes_and_ignores_line_drift_and_order(self):
        base = ratchet.summarize([dup("a.ts", "b.ts", "x = 1\n\ny = 2")])
        cur = ratchet.summarize([dup("b.ts", "a.ts", "  x = 1\ny = 2", lines=99)])
        self.assertEqual(ratchet.clone_failures(cur, base), [])

    def test_fixed_clone_is_stale(self):
        base = ratchet.summarize([dup("a.ts", "b.ts", "x = 1")])
        f = ratchet.clone_failures({}, base)
        self.assertTrue(f and f[0].startswith("STALE:"))

    def test_missing_fragment_is_an_error_not_zero(self):
        with self.assertRaises(RuntimeError):
            ratchet.clone_signature(dup("a.ts", "b.ts", ""))

    def test_write_baseline_growth_detected(self):
        old = ratchet.summarize([dup("a.ts", "b.ts", "x")])
        new = ratchet.summarize([dup("a.ts", "b.ts", "x"), dup("a.ts", "b.ts", "x"), dup("c.ts", "d.ts", "z")])
        self.assertEqual(len(ratchet.growth(new, old, True)), 2)
        self.assertEqual(ratchet.growth(old, new, True), [])


if __name__ == "__main__":
    unittest.main()
