#!/bin/zsh
# RED evidence for STOMPY-1991 (an ADDRESS never rides the raw actor field) —
# reproducible, and SELF-VERIFYING.
#
# Each revert is NAMED, applied to the working tree, run, and undone. The
# script fails if a revert's needle is not found (a drifted needle would
# silently no-op and still print "RED"), if pytest cannot run, or if a revert
# leaves the suite GREEN — a test asserting an outcome the broken code also
# produces is exactly what this script exists to catch.
#
# Usage, from anywhere in the repo:
#   zsh scripts/red/red_1991_raw_actor.sh
set -e
set -o pipefail          # a gate's exit code must not be `tail`'s (2026-09-02)
PY="${PY:-python3}"
cd "$(git rev-parse --show-toplevel)"
B="$(mktemp -d)"
U="tests/test_no_raw_email_actor_1991.py"
M="tests/test_ticket_authors_1594.py"
FAILURES=0
CURRENT=""

restore() { for f in $B/*.bak(N); do cp "$f" "$(cat "$f.path")"; done }
trap restore EXIT INT TERM
undo() { restore; rm -f $B/*.bak(N) $B/*.path(N) }

apply_stdin() {
  local snippet="$B/snippet.py"
  cat > "$snippet"
  $PY "$B/driver.py" "$snippet" "$B" || { echo "!! REVERT SETUP FAILED — $CURRENT"; exit 1 }
}

cat > "$B/driver.py" <<'DRIVER'
import pathlib, re, shutil, sys

snippet, backup_dir = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
source = snippet.read_text()
match = re.search(r'^p\s*=\s*"([^"]+)"', source, re.M)
if not match:
    sys.exit('revert snippet does not name its file as p = "..."')
target = pathlib.Path(match.group(1))
before = target.read_text()
key = str(target).replace("/", "_")
shutil.copy(target, backup_dir / (key + ".bak"))
(backup_dir / (key + ".bak.path")).write_text(str(target))
exec(compile(source, str(snippet), "exec"), {"__name__": "__main__"})
if target.read_text() == before:
    sys.exit(f"NEEDLE NOT FOUND in {target} — the revert changed nothing")
DRIVER

expect_red() {
  local label="$1"; shift
  local out="$B/out.txt"
  if $PY -m pytest "$@" -q -p no:cacheprovider  > "$out" 2>&1; then
    echo "!! STILL GREEN: $label — the test does not detect this revert"
    FAILURES=$((FAILURES + 1))
  elif grep -qE "error(s)? during collection|ImportError|SyntaxError" "$out"; then
    echo "!! BROKEN, NOT RED: $label — the revert did not compile"
    tail -5 "$out"
    FAILURES=$((FAILURES + 1))
  else
    grep -E "^FAILED|failed," "$out" | tail -8
  fi
}

CURRENT="===== REVERT 1 (THE SHIPPED STATE of origin/main): only the *_display fields are filled"
echo "$CURRENT ====="
apply_stdin <<'EOF'
p = "stompy_ticketing/mcp_tools.py"
s = open(p).read()
s = s.replace("        return redact_actors(ticket, names)", "        return ticket")
s = s.replace("            return redact_actors(ticket, {})", "            return ticket")
s = s.replace("        if not ids or not display_actors_func:\n            return redact_actors(ticket, {})",
              "        if not ids or not display_actors_func:\n            return ticket")
open(p, "w").write(s)
EOF
expect_red "$CURRENT" $U $M
undo

echo
CURRENT="===== REVERT 2: the placeholder becomes the address itself"
echo "$CURRENT ====="
apply_stdin <<'EOF'
p = "stompy_ticketing/actors.py"
s = open(p).read()
s = s.replace('    return (names or {}).get(value) or ADDRESS_PLACEHOLDER', "    return value")
open(p, "w").write(s)
EOF
expect_red "$CURRENT" $U
undo

echo
CURRENT="===== REVERT 3 (THE DEGRADED PATH): a raising host resolver stops redacting"
echo "$CURRENT ====="
apply_stdin <<'EOF'
p = "stompy_ticketing/mcp_tools.py"
s = open(p).read()
s = s.replace("        except Exception:\n            return redact_actors(ticket, {})",
              "        except Exception:\n            return ticket")
open(p, "w").write(s)
EOF
expect_red "$CURRENT" $U
undo

echo
CURRENT="===== REVERT 4 (THE OTHER DOOR): REST stops redacting, every shape"
echo "$CURRENT ====="
apply_stdin <<'EOF'
p = "stompy_ticketing/actors.py"
s = open(p).read()
# The boundary itself becomes a pass-through: get, update, move, board and
# search all go raw at once, which is the point of having ONE boundary.
s = s.replace("    if payload is None or _depth > 6:\n        return payload",
              "    if True:\n        return payload")
open(p, "w").write(s)
EOF
expect_red "$CURRENT" $U
undo

echo
CURRENT="===== REVERT 4b (THE FORGOTTEN HANDLER): board and search skip the boundary"
echo "$CURRENT ====="
apply_stdin <<'EOF'
p = "stompy_ticketing/api_routes.py"
s = open(p).read()
# Exactly the shape of the original bug: the DETAIL route is redacted and the
# list-shaped ones are not, which is how the door was forgotten once already.
s = s.replace("        return redact_payload(\n            _service.board_view(", "        return (\n            _service.board_view(")
s = s.replace("        return redact_payload(\n            _service.search_tickets(", "        return (\n            _service.search_tickets(")
open(p, "w").write(s)
EOF
expect_red "$CURRENT" $U
undo

echo
CURRENT="===== REVERT 5: a NUMERIC id is redacted too (the fix must not erase real identity)"
echo "$CURRENT ====="
apply_stdin <<'EOF'
p = "stompy_ticketing/actors.py"
s = open(p).read()
s = s.replace('    if not isinstance(value, str) or "@" not in value:\n        return value\n', "")
open(p, "w").write(s)
EOF
expect_red "$CURRENT" $U $M
undo

echo
echo "===== RESTORED — confirming GREEN ====="
undo
$PY -m pytest tests -q -p no:cacheprovider 2>&1 | tail -3

if [[ $FAILURES -gt 0 ]]; then
  echo
  echo "$FAILURES revert(s) did not produce honest RED — this is NOT evidence."
  exit 1
fi
echo
echo "all reverts RED, restored tree GREEN"
