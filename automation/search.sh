#!/usr/bin/env bash
# Search chosen websites for a subject, on demand ("Search sites" workflow): crawl the sites and
# their sub-sites following links about the keywords first, check the matching books on Amazon,
# and print the table. It starts from the weekly run's database but doesn't save to it.
# Run from the repository root:
#   SITES="nasa.gov history.nasa.gov" KEYWORDS="Apollo 13, Apollo XIII" automation/search.sh
set -uo pipefail

DATA="${DATA_DIR:-data}"
CONFIG="${GOVBOOKS_CONFIG:-automation/govbooks.toml}"
OUT="${OUT_DIR:-results}"
SITES="${SITES:?set SITES, e.g. nasa.gov}"
KEYWORDS="${KEYWORDS:?set KEYWORDS, e.g. Apollo 13}"
PAGES="${PAGES:-400}"        # pages per site
MINUTES="${MINUTES:-40}"     # minutes per site at most
SUBSITES="${SUBSITES:-20}"   # sub-sites searched too
SUBSITE_PAGES="${SUBSITE_PAGES:-150}"
if [ -z "${AMAZON_PROVIDER:-}" ]; then
  if [ -n "${KEEPA_API_KEY:-}" ]; then AMAZON_PROVIDER=keepa; else AMAZON_PROVIDER=catalog; fi
fi

sites=()
set -f
for site in $SITES; do
  if [[ "$site" =~ ^[A-Za-z0-9][A-Za-z0-9.:/_-]*$ ]]; then sites+=("$site"); else echo "Ignoring site '$site'"; fi
done
set +f
[ "${#sites[@]}" -gt 0 ] || { echo "No valid sites."; exit 1; }

mkdir -p "$DATA" "$OUT"
web=(govweb --db "$DATA/govweb.db")
narrow=(--sites "${sites[@]}" --keywords "$KEYWORDS")

"${web[@]}" sync || true  # the .gov map, so sub-sites are recognized
"${web[@]}" crawl "${sites[@]}" --keywords "$KEYWORDS" --max-pages "$PAGES" --max-depth 5 \
  --max-minutes "$MINUTES" --with-subsites --max-subsites "$SUBSITES" --subsite-pages "$SUBSITE_PAGES" \
  --skip-recent-days 0 --workers 8
"${web[@]}" amazon "${narrow[@]}" --provider "$AMAZON_PROVIDER" --limit 500 --config "$CONFIG" --verbose
"${web[@]}" export "${narrow[@]}" --all-documents --config "$CONFIG" --out "$OUT/books.csv"
"${web[@]}" export "${narrow[@]}" --all-documents --config "$CONFIG" --out "$OUT/books.md" --rows 300 \
  --heading "Documents about \"$KEYWORDS\" on ${sites[*]}"

cat "$OUT/books.md"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then cat "$OUT/books.md" >> "$GITHUB_STEP_SUMMARY"; fi
