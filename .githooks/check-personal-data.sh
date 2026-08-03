#!/bin/bash
# Refuses a commit that would publish personal data.
#
# This repo keeps real statements (data/, inbox/) beside public source, and layout
# examples get copied out of real PDFs while writing parsers and tests. Those files
# are NOT gitignored — they are source — so the tripwire in run-tests.sh does not
# cover them. CONTRIBUTING.md requires synthetic names and IBANs; this enforces it.
#
# An IBAN cannot be rotated the way a leaked key can, so this fails closed.
set -u

staged=$(git diff --cached --name-only --diff-filter=ACM)
[ -z "$staged" ] && exit 0

problems=0
report() {   # file : reason : sample
  printf '  %s\n      %s: %s\n' "$1" "$2" "$3"
  problems=1
}

# Authorship is deliberate in these; everything else is suspect.
is_authorship_file() {
  case "$1" in
    LICENSE|README.md|CONTRIBUTING.md|CODE_OF_CONDUCT.md|SECURITY.md|CHANGELOG.md) return 0 ;;
    docs/*|.github/*) return 0 ;;
    *) return 1 ;;
  esac
}

# Names to look for come from the local config, so this adapts to whoever runs it
# and never hardcodes anybody. config.json is gitignored and stays local.
names=""
if [ -f config.json ]; then
  names=$(python3 - <<'PY' 2>/dev/null
import json, re
try:
    cfg = json.load(open("config.json"))
except Exception:
    raise SystemExit
words = set()
for value in list((cfg.get("person_labels") or {}).values()) + [cfg.get("household_name") or ""]:
    for part in re.split(r"[^\wÀ-ÿ]+", str(value)):
        if len(part) >= 4:
            words.add(part.lower())
print("\n".join(sorted(words)))
PY
)
fi

for file in $staged; do
  [ -f "$file" ] || continue
  case "$file" in *.png|*.jpg|*.jpeg|*.gif|*.pdf|*.zip|*.woff*|*.ttf) continue ;; esac
  added=$(git diff --cached -U0 -- "$file" | grep '^+' | grep -v '^+++')
  [ -z "$added" ] && continue

  # An IBAN with real variety in its digits. Synthetic placeholders in this repo
  # repeat one digit (DE111..., DE222...), so they pass.
  while read -r iban; do
    [ -z "$iban" ] && continue
    digits=${iban#??}
    distinct=$(printf '%s' "$digits" | fold -w1 | sort -u | wc -l | tr -d ' ')
    [ "$distinct" -le 2 ] && continue
    report "$file" "looks like a real IBAN" "$iban"
  done <<< "$(printf '%s' "$added" | grep -ohE '\b[A-Z]{2}[0-9]{16,32}\b' | sort -u)"

  is_authorship_file "$file" && continue
  for name in $names; do
    match=$(printf '%s' "$added" | grep -ohiE "\b$name\b" | head -1)
    [ -n "$match" ] && report "$file" "household name from config.json" "$match"
  done
done

if [ "$problems" -ne 0 ]; then
  cat >&2 <<'MSG'

COMMIT BLOCKED — personal data in the staged changes (listed above).

Use synthetic names and IBANs in code, docs and tests (CONTRIBUTING.md). This repo
is public, and an IBAN cannot be rotated once published.

If a hit is deliberate, commit with --no-verify and say why in the message.
MSG
  exit 1
fi
exit 0
