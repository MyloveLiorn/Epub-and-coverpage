#!/usr/bin/env bash
# One state of the "Search states" workflow: search the state's own websites for the topics in
# govbooks.toml (the state's portal first, then sites named like libraries, archives or the
# topic), count the pages of what was found, check Amazon, and keep the state's best books.
# Run from the repository root:
#   STATE=TX automation/state-search.sh
# Writes results/<STATE>-top.csv (the best books), <STATE>-sheet.csv (the same in the Google
# Sheet layout), <STATE>-all.csv (every document on the topics) and <STATE>-summary.txt.
set -uo pipefail

DATA="${DATA_DIR:-data}"
CONFIG="${GOVBOOKS_CONFIG:-automation/govbooks.toml}"
OUT="${OUT_DIR:-results}"
STATE="${STATE:?set STATE, e.g. TX}"
TOP="${TOP:-5}"                          # books kept per state
MIN_PAGES="${MIN_PAGES:-10}"             # shorter documents aren't books (uncounted ones stay)
SITES_PER_STATE="${SITES_PER_STATE:-30}"
SUBSITES="${SUBSITES:-20}"               # agency sites found under the portal (dhs.georgia.gov, ...)
PAGES="${PAGES:-150}"                    # pages per site
MINUTES="${MINUTES:-6}"                  # minutes per site at most
PAGE_COUNTS="${PAGE_COUNTS:-150}"
# Parts of site names searched first: libraries, archives and history sites publish books;
# refugee and "new Americans" offices, and the human-services departments that run them, publish
# about immigration. Courts, lotteries and the like ("-court") go last.
PREFER_NAMES="${PREFER_NAMES:-librar,archiv,histor,refugee,immigra,newamerican,humanservice,dhs,dss,dhhs,-court,-appeal,-probate,-judicial,-jqc,-lottery,-sheriff,-racing,-gaming,-elect,-vote}"
if [ -z "${AMAZON_PROVIDER:-}" ]; then
  if [ -n "${KEEPA_API_KEY:-}" ]; then AMAZON_PROVIDER=keepa; else AMAZON_PROVIDER=catalog; fi
fi

STATE="${STATE^^}"
[[ "$STATE" =~ ^[A-Z]{2}$ ]] || { echo "Not a state code: $STATE"; exit 1; }
mkdir -p "$DATA" "$OUT"
web=(govweb --db "$DATA/govweb.db")
topics=(--config "$CONFIG")
subject="$(sed -n 's/^\[topics\.\(.*\)\]$/\1/p' "$CONFIG" | paste -sd, | sed 's/,/, /g')"

"${web[@]}" sync || true
"${web[@]}" portals "$STATE"
"${web[@]}" crawl --level state --state "$STATE" --limit "$SITES_PER_STATE" --all-topics "${topics[@]}" \
  --prefer-names "$PREFER_NAMES" --skip-recent-days 0 --max-pages "$PAGES" --max-depth 4 \
  --max-minutes "$MINUTES" --workers 8 \
  --with-subsites --max-subsites "$SUBSITES" --subsite-pages 100 --subsite-minutes 5
"${web[@]}" count-pages --state "$STATE" --topics-only "${topics[@]}" --limit "$PAGE_COUNTS"
"${web[@]}" amazon --state "$STATE" --topics-only "${topics[@]}" --provider "$AMAZON_PROVIDER" --limit 100

best=(--state "$STATE" --topics-only "${topics[@]}" --sort pages)
"${web[@]}" export "${best[@]}" --top "$TOP" --min-pages "$MIN_PAGES" --out "$OUT/$STATE-top.csv"
"${web[@]}" export "${best[@]}" --top "$TOP" --min-pages "$MIN_PAGES" --out "$OUT/$STATE-sheet.csv" \
  --sheet "$subject: $STATE"
"${web[@]}" export "${best[@]}" --out "$OUT/$STATE-all.csv"

found=$(( $(wc -l < "$OUT/$STATE-all.csv") - 1 ))
kept=$(( $(wc -l < "$OUT/$STATE-top.csv") - 1 ))
echo "$STATE: $kept of $found book-like document(s) on $subject kept" | tee "$OUT/$STATE-summary.txt"
