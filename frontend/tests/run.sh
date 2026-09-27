#!/usr/bin/env bash
# Run the frontend module tests headlessly. Needs chromium; no npm packages.
#   frontend/tests/run.sh            all tests
#   frontend/tests/run.sh store      tests whose file name contains "store"
set -u
cd "$(dirname "$0")"
BIN="${CHROMIUM:-$(command -v chromium || command -v chromium-browser || command -v google-chrome || true)}"
[ -n "$BIN" ] || { echo "chromium not found (set CHROMIUM=/path/to/chromium)"; exit 2; }
filter="${1:-}"
pass=0; fail=0; files=0
for t in *.test.html; do
  [ -z "$filter" ] || [[ "$t" == *"$filter"* ]] || continue
  files=$((files + 1))
  out="$(timeout 120 "$BIN" --headless=new --disable-gpu --no-sandbox --allow-file-access-from-files \
        --virtual-time-budget=8000 --dump-dom "file://$PWD/$t" 2>/dev/null \
        | sed 's/.*<pre id="out">//; s/<\/pre>.*//' | sed 's/&gt;/>/g; s/&lt;/</g; s/&amp;/\&/g' | grep -E '^(PASS|FAIL) ')"
  if [ -z "$out" ]; then echo "FAIL $t: no results (page error?)"; fail=$((fail + 1)); continue; fi
  p=$(grep -c '^PASS' <<<"$out"); f=$(grep -c '^FAIL' <<<"$out")
  pass=$((pass + p)); fail=$((fail + f))
  printf '%-28s %3d passed %3d failed\n' "$t" "$p" "$f"
  grep '^FAIL' <<<"$out" | sed 's/^/    /'
done
echo "$files files: $pass passed, $fail failed"
[ "$fail" -eq 0 ] && [ "$files" -gt 0 ]
