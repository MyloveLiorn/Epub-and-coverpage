#!/usr/bin/env bash
# One state of the "Search states" workflow: search the state's own websites for one topic of
# govbooks.toml (the state's portal first, then sites named like libraries, archives or the
# topic), count the pages of what was found, check Amazon, and keep the state's best books.
# Run from the repository root:
#   STATE=TX TOPIC=immigration automation/state-search.sh
# Writes results/<STATE>-top.csv (the best books), <STATE>-sheet.csv (the same in the Google
# Sheet layout), <STATE>-all.csv (every document on the topic) and <STATE>-summary.txt.
set -uo pipefail

DATA="${DATA_DIR:-data}"
CONFIG="${GOVBOOKS_CONFIG:-automation/govbooks.toml}"
OUT="${OUT_DIR:-results}"
STATE="${STATE:?set STATE, e.g. TX}"
TOPIC="${TOPIC:?set TOPIC, a [topics.NAME] of govbooks.toml, e.g. immigration}"
TOP="${TOP:-5}"                          # books kept per state
MIN_PAGES="${MIN_PAGES:-18}"             # books have 18 pages or more (uncounted documents are left out)
SITES_PER_STATE="${SITES_PER_STATE:-30}"
SUBSITES="${SUBSITES:-20}"               # agency sites found under the portal (dhs.georgia.gov, ...)
PAGES="${PAGES:-150}"                    # pages per site
MINUTES="${MINUTES:-6}"                  # minutes per site at most
PAGE_COUNTS="${PAGE_COUNTS:-150}"
# Parts of site names searched first: libraries, archives and history sites publish books, and
# the topic's own (prefer_sites in govbooks.toml: "refugee" for immigration, "police" for
# law-enforcement careers). Courts, lotteries and the like go last, unless the topic prefers them.
FIRST_NAMES="librar,archiv,histor"
LAST_NAMES="court,appeal,probate,judicial,jqc,lottery,sheriff,racing,gaming,elect,vote,app,login,licens,complaint,careers,jobs,gis,pay"
if [ -z "${AMAZON_PROVIDER:-}" ]; then
  if [ -n "${KEEPA_API_KEY:-}" ]; then AMAZON_PROVIDER=keepa; else AMAZON_PROVIDER=catalog; fi
fi

STATE="${STATE^^}"
[[ "$STATE" =~ ^[A-Z]{2}$ ]] || { echo "Not a state code: $STATE"; exit 1; }
grep -qxF "[topics.$TOPIC]" "$CONFIG" || { echo "No topic $TOPIC in $CONFIG"; exit 1; }
mkdir -p "$DATA" "$OUT"
web=(govweb --db "$DATA/govweb.db")
# The config without the other topics, so they neither steer the crawl nor share the results.
one="$DATA/topic-$TOPIC.toml"
awk -v t="[topics.$TOPIC]" '/^\[/ { keep = ($0 !~ /^\[topics\./) || ($0 == t) } keep' "$CONFIG" > "$one"
topics=(--config "$one")
subject="$TOPIC"
if [ -z "${PREFER_NAMES:-}" ]; then
  topic_names="$(python3 - "$one" "$TOPIC" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    print(",".join(tomllib.load(fh)["topics"][sys.argv[2]].get("prefer_sites", [])))
PY
)"
  PREFER_NAMES="$FIRST_NAMES${topic_names:+,$topic_names}"
  IFS=',' read -r -a last <<< "$LAST_NAMES"
  for name in "${last[@]}"; do
    [[ ",$topic_names," == *",$name,"* ]] || PREFER_NAMES+=",-$name"
  done
fi
echo "Topic $TOPIC; sites named like these first (- last): $PREFER_NAMES"

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
echo "$STATE: $kept of $found book-like document(s) on $subject kept ($MIN_PAGES+ pages)" | tee "$OUT/$STATE-summary.txt"
