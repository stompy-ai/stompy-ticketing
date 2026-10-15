#!/usr/bin/env python3
"""Shrink-only file-size and code-clone ratchets (STOMPY-2811).

One stdlib-only script, vendored byte-identically into stompy-web,
stompy-ticketing and stompy-cli; each repo's policy lives in its own
``ratchet_config.json``. Same rules as the backend gates
(dementia-production scripts/check_file_size_ladder.py, check_code_clones.py):

SIZE   a source file over FLOOR (500) lines may never GROW past its recorded
       baseline; a NEW file over FLOOR fails (new modules are born small);
       a shrink that is not recorded (or a deleted file) fails as STALE.
CLONES a clone signature not in the baseline fails (NEW); a signature whose
       pair count grows fails (RATCHET); one that shrank/vanished fails
       (STALE). Signatures hash file pair + whitespace-normalised fragment,
       no line numbers. A broken jscpd scan is an error, never "no clones".

``--write-baseline`` only SHRINKS: it refuses to add a key or raise a value
(first creation, when no baseline exists yet, is the one exception).

Usage:
    python3 scripts/ratchet.py size   [--root DIR] [--write-baseline]
    python3 scripts/ratchet.py clones [--root DIR] [--write-baseline]
"""

import fnmatch
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

FLOOR = 500
JSCPD_VERSION = "5.1.0"  # pinned (backend STOMPY-1713 precedent) -- bump deliberately
SIZE_BASELINE = "file_size_baseline.json"
CLONE_BASELINE = "code_clone_baseline.json"
CONFIG = "ratchet_config.json"


def load_config(root: Path) -> dict:
    cfg = json.loads((root / CONFIG).read_text())
    for entry in cfg.get("exclude", []):
        if not entry.get("reason") or not entry.get("class") or not entry.get("glob"):
            raise ValueError(f"exclude entry needs glob+class+reason: {entry}")
    return cfg


def _glob_match(rel: str, pattern: str) -> bool:
    if "/" not in pattern:
        return fnmatch.fnmatch(rel.rsplit("/", 1)[-1], pattern)
    return fnmatch.fnmatch(rel, pattern)


def is_excluded(rel: str, cfg: dict) -> bool:
    parts = set(rel.split("/")[:-1])
    if parts & set(cfg.get("ignore_dirs", [])):
        return True
    return any(_glob_match(rel, e["glob"]) for e in cfg.get("exclude", []))


def measure(root: Path, cfg: dict) -> dict:
    exts = tuple(cfg["extensions"])
    sizes = {}
    for path in root.rglob("*"):
        if not path.is_file() or not path.name.endswith(exts):
            continue
        rel = path.relative_to(root).as_posix()
        if is_excluded(rel, cfg):
            continue
        with path.open(encoding="utf-8", errors="replace") as fh:
            lines = sum(1 for _ in fh)
        if lines > FLOOR:
            sizes[rel] = lines
    return dict(sorted(sizes.items()))


def size_failures(current: dict, baseline: dict) -> list:
    failures = []
    for rel, lines in current.items():
        recorded = baseline.get(rel)
        if recorded is None:
            failures.append(
                f"NEW: {rel} is a new file over {FLOOR} lines ({lines}) -- new modules are born small"
            )
        elif lines > recorded:
            failures.append(f"RATCHET: {rel} grew {recorded} -> {lines}; shrink it or move new code to a module under {FLOOR}")
        elif lines < recorded:
            failures.append(f"STALE: {rel} baseline records {recorded} but the file is {lines} -- run --write-baseline")
    for rel, recorded in baseline.items():
        if rel not in current:
            failures.append(f"STALE: {rel} baseline records {recorded} but it is gone or under {FLOOR} -- run --write-baseline")
    return failures


# ---- clones -----------------------------------------------------------------

def jscpd_ignores(cfg: dict) -> list:
    globs = [f"**/{d}/**" for d in cfg.get("ignore_dirs", [])]
    for e in cfg.get("exclude", []):
        g = e["glob"]
        globs.append(g if "/" in g else f"**/{g}")
    return globs


def run_jscpd(root: Path, cfg: dict, timeout: int = 600) -> dict:
    clone = cfg["clone"]
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["npx", "--yes", f"jscpd@{JSCPD_VERSION}", ".", "--min-tokens", str(clone.get("min_tokens", 70)),
               "--reporters", "json", "--output", tmp, "--format", clone["format"],
               "-i", ",".join(jscpd_ignores(cfg)), "--silent", "--no-tips"]
        try:
            res = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise RuntimeError("npx not found -- jscpd needs Node.js on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"jscpd timed out after {timeout}s") from exc
        if res.returncode != 0:
            raise RuntimeError(f"jscpd exited {res.returncode}:\n{res.stdout}\n{res.stderr}")
        report = Path(tmp) / "jscpd-report.json"
        if not report.exists():
            raise RuntimeError("jscpd produced no report -- refusing to treat 'no report' as 'no clones'")
        return json.loads(report.read_text())


def normalize_fragment(text: str) -> str:
    return "\n".join(s for s in (ln.strip() for ln in text.splitlines()) if s)


def clone_signature(dup: dict) -> str:
    a, b = sorted((dup["firstFile"]["name"], dup["secondFile"]["name"]))
    fragment = dup.get("fragment")
    if not fragment:
        raise RuntimeError(f"jscpd clone {a} <-> {b} has no 'fragment' text -- jscpd schema drift?")
    digest = hashlib.sha256(f"{a}\x00{b}\x00{normalize_fragment(fragment)}".encode()).hexdigest()[:16]
    return f"{a}::{b}::{digest}"


def summarize(duplicates: list) -> dict:
    out: dict = {}
    for dup in sorted(duplicates, key=clone_signature):
        sig = clone_signature(dup)
        if sig not in out:
            a, b = sorted((dup["firstFile"]["name"], dup["secondFile"]["name"]))
            out[sig] = {"files": [a, b], "count": 0, "lines": dup.get("lines", 0), "tokens": dup.get("tokens", 0)}
        out[sig]["count"] += 1
    return dict(sorted(out.items()))


def clone_failures(current: dict, baseline: dict) -> list:
    failures = []
    for sig, info in current.items():
        a, b = info["files"]
        rec = baseline.get(sig)
        if rec is None:
            failures.append(f"NEW: {a} <-> {b} ({info['lines']} lines, {info['tokens']} tokens) -- clone not in baseline")
        elif info["count"] > rec["count"]:
            failures.append(f"RATCHET: {a} <-> {b} grew {rec['count']} -> {info['count']} occurrences")
    for sig, rec in baseline.items():
        cur = current.get(sig)
        if cur is None or cur["count"] < rec["count"]:
            a, b = rec["files"]
            failures.append(f"STALE: {a} <-> {b} recorded but no longer reproduced (or shrank) -- run --write-baseline")
    return failures


# ---- shared driver ----------------------------------------------------------

def growth(new: dict, old: dict, clones: bool) -> list:
    """Keys a --write-baseline would ADD or RAISE; empty means it is a shrink."""
    bad = []
    for k, v in new.items():
        if k not in old:
            bad.append(k)
        elif (v["count"] > old[k]["count"]) if clones else (v > old[k]):
            bad.append(k)
    return sorted(bad)


def main(argv: list) -> int:
    if len(argv) < 2 or argv[1] not in ("size", "clones"):
        print(__doc__)
        return 2
    mode = argv[1]
    root = Path(argv[argv.index("--root") + 1]).resolve() if "--root" in argv else Path(__file__).resolve().parent.parent
    cfg = load_config(root)
    clones = mode == "clones"
    baseline_path = root / (CLONE_BASELINE if clones else SIZE_BASELINE)
    try:
        current = summarize(run_jscpd(root, cfg).get("duplicates", [])) if clones else measure(root, cfg)
    except RuntimeError as exc:
        print(f"{mode} scan failed: {exc}")
        return 1
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else None
    if "--write-baseline" in argv:
        if baseline is not None and (bad := growth(current, baseline, clones)):
            print("refusing to grow the baseline:", *bad, sep="\n  ")
            return 1
        baseline_path.write_text(json.dumps(current, indent=1) + "\n")
        print(f"baseline written: {len(current)} grandfathered {'clone signatures' if clones else 'files'}")
        return 0
    failures = (clone_failures if clones else size_failures)(current, baseline or {})
    if failures:
        print(f"{'Code-clone' if clones else 'File-size'} ratchet violations (STOMPY-2811):")
        print(*("  " + f for f in sorted(failures)), sep="\n")
        return 1
    print(f"{mode} ratchet ok: {len(current)} grandfathered, none grew")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
