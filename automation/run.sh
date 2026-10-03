#!/usr/bin/env bash
# The weekly job: map the government websites, search the priority sites (army.mil, ...) and
# their sub-sites in depth, search the websites of a few states in depth (every state in turn),
# crawl the next batch of federal sites, check Amazon, and write the results tables. Crawls
# follow links about the topics in govbooks.toml first.
# Run from the repository root. Every step runs even if an earlier one fails; the script exits
# non-zero at the end if any step failed.
set -uo pipefail

DATA="${DATA_DIR:-data}"
CONFIG="${GOVBOOKS_CONFIG:-automation/govbooks.toml}"
PRIORITY_FILE="${PRIORITY_SITES_FILE:-automation/priority-sites.txt}"
EXTRA_SITES="${EXTRA_SITES:-}"         # more sites to search in depth, this run only
PRIORITY_PAGES="${PRIORITY_PAGES:-400}" # pages per priority site
SUBSITES="${SUBSITES:-40}"              # sub-sites of the priority sites crawled per run
SUBSITE_PAGES="${SUBSITE_PAGES:-120}"   # pages per sub-site
FEDERAL_SITES="${FEDERAL_SITES:-150}"
STATES="${STATES:-}"                    # states searched in depth this run, e.g. "CA TX"; empty: the next in turn
STATES_PER_RUN="${STATES_PER_RUN:-5}"   # 5 a week covers all 50 states every 10 weeks
STATE_SITES="${STATE_SITES:-40}"        # sites per state per run
STATE_PAGES="${STATE_PAGES:-150}"       # pages per state site
AMAZON_CHECKS="${AMAZON_CHECKS:-300}"
STEP_TIMEOUT="${STEP_TIMEOUT:-45m}"  # no single step may hang the run
# Minutes one site may take. army.mil asks for about 5 seconds between pages, so 400 pages
# take ~35 minutes; each step must fit in STEP_TIMEOUT.
PRIORITY_MINUTES="${PRIORITY_MINUTES:-40}"
SUBSITE_MINUTES="${SUBSITE_MINUTES:-8}"
STATE_SITE_MINUTES="${STATE_SITE_MINUTES:-6}"
SITE_MINUTES="${SITE_MINUTES:-5}"
if [ -z "${AMAZON_PROVIDER:-}" ]; then
  if [ -n "${KEEPA_API_KEY:-}" ]; then AMAZON_PROVIDER=keepa; else AMAZON_PROVIDER=catalog; fi
fi

mkdir -p "$DATA"
# Results of the book-catalog search (Internet Archive, Google Books, ...), which the weekly
# run no longer does.
rm -f "$DATA/catalog-books.csv" "$DATA/govbooks.db" "$DATA/govbooks.db.agencies-done"

failed=()
step() {
  echo "::group::$*"
  local rc=0
  timeout "$STEP_TIMEOUT" "$@" || rc=$?
  if [ "$rc" -ne 0 ]; then
    local why="exit $rc"
    [ "$rc" -eq 124 ] && why="timed out after $STEP_TIMEOUT"
    echo "::warning::Step failed ($why): $*"
    failed+=("$*")
  fi
  echo "::endgroup::"
  return "$rc"
}

web=(govweb --db "$DATA/govweb.db")
topics=(--all-topics --config "$CONFIG")

# 1. The map: the .gov registry and agency websites. The agency directories change slowly:
#    refresh them in the first week of each month, or when the database is new.
step "${web[@]}" sync
if [ ! -f "$DATA/govweb.db.agencies-done" ] || [ "$(date -u +%-d)" -le 7 ]; then
  step "${web[@]}" agencies sync && date -u +%F > "$DATA/govweb.db.agencies-done"
fi

# 2. The priority sites, in depth, with the next batch of their sub-sites.
priority=()
set -f  # a "*" in the list must not expand to file names
for site in $(sed -e 's/#.*//' "$PRIORITY_FILE" 2>/dev/null) $EXTRA_SITES; do
  if [[ "$site" =~ ^[A-Za-z0-9][A-Za-z0-9.:/_-]*$ ]]; then priority+=("$site"); else echo "Ignoring site '$site'"; fi
done
set +f
if [ "${#priority[@]}" -gt 0 ]; then
  step "${web[@]}" crawl "${priority[@]}" "${topics[@]}" --max-pages "$PRIORITY_PAGES" --max-depth 4 \
    --max-minutes "$PRIORITY_MINUTES" --workers 8
  step "${web[@]}" crawl "${priority[@]}" "${topics[@]}" --subsites-only --max-subsites "$SUBSITES" \
    --max-pages "$SUBSITE_PAGES" --max-depth 4 --max-minutes "$SUBSITE_MINUTES" --workers 8
fi

# 3. State websites in depth: a few states each run, every state in turn (by week of the
#    year), or the states asked for. In each state, the state's official website comes first,
#    then sites never searched, those whose names suggest books (library, archives, history,
#    geology, ...) leading.
ALL_STATES=(AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO
            MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY)
# States whose publications can't be reused are left out (govweb/data/excluded_states.txt).
skip=" $(sed -e 's/#.*//' govweb/data/excluded_states.txt | tr '\n' ' ') "
usable=()
for st in "${ALL_STATES[@]}"; do [[ "$skip" == *" $st "* ]] || usable+=("$st"); done
ALL_STATES=("${usable[@]}")
states=()
for st in $STATES; do
  if [[ ! "$st" =~ ^[A-Za-z]{2}$ ]]; then echo "Ignoring state '$st'"
  elif [[ "$skip" == *" ${st^^} "* ]]; then echo "Skipping ${st^^}: its publications can't be reused"
  else states+=("${st^^}"); fi
done
if [ "${#states[@]}" -eq 0 ]; then
  start=$(( (10#$(date -u +%V) * STATES_PER_RUN) % ${#ALL_STATES[@]} ))
  for ((i = 0; i < STATES_PER_RUN; i++)); do states+=("${ALL_STATES[$(( (start + i) % ${#ALL_STATES[@]} ))]}"); done
fi
echo "States this run: ${states[*]}"
for st in "${states[@]}"; do
  # The state's official website (texas.gov, ...) goes first: it links to the state's agencies.
  step "${web[@]}" portals "$st"
  step "${web[@]}" crawl --level state --state "$st" --limit "$STATE_SITES" "${topics[@]}" \
    --max-pages "$STATE_PAGES" --max-depth 4 --max-minutes "$STATE_SITE_MINUTES" --workers 8
done

# 4. The next batch of federal sites (sites crawled in the last 30 days wait).
step "${web[@]}" crawl --level federal --limit "$FEDERAL_SITES" "${topics[@]}" --max-pages 60 \
  --max-minutes "$SITE_MINUTES" --workers 8

# 5. Amazon: is each book already sold there? Books on the topics are checked first.
step "${web[@]}" amazon --provider "$AMAZON_PROVIDER" --limit "$AMAZON_CHECKS" --config "$CONFIG"
step "${web[@]}" count-pages --topics-only --config "$CONFIG" --limit 150

# 6. The results tables.
step "${web[@]}" export --config "$CONFIG" --out "$DATA/topic-books.csv" --topics-only
step "${web[@]}" export --config "$CONFIG" --out "$DATA/new-books.csv" --new-only --days 8
step "${web[@]}" export --config "$CONFIG" --out "$DATA/website-books.csv"
step "${web[@]}" export --config "$CONFIG" --out "$DATA/last-run.md" --days 1 \
  --heading "Books found in this run (topic books first)"
step "${web[@]}" stats
if [ -n "${GITHUB_STEP_SUMMARY:-}" ] && [ -f "$DATA/last-run.md" ]; then
  cat "$DATA/last-run.md" >> "$GITHUB_STEP_SUMMARY"
fi

# 7. Google Sheet, when its secrets are set: a tab per topic (books of 18 pages or more), and the new books.
if [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON:-}" ] && [ -n "${GOOGLE_SHEET_ID:-}" ]; then
  tabs=()
  for topic in $(sed -n 's/^\[topics\.\(.*\)\]$/\1/p' "$CONFIG"); do
    awk -v t="[topics.$topic]" '/^\[/ { keep = ($0 !~ /^\[topics\./) || ($0 == t) } keep' "$CONFIG" \
      > "$DATA/topic-$topic.toml"
    step "${web[@]}" export --config "$DATA/topic-$topic.toml" --out "$DATA/sheet-$topic.csv" --topics-only \
      --min-pages 18 --sheet "Weekly: $topic"
    tabs+=(--tab "Topic books - $topic=$DATA/sheet-$topic.csv")
  done
  step "${web[@]}" export --config "$CONFIG" --out "$DATA/sheet-new.csv" --new-only --days 8 --sheet "Weekly: new"
  step "${web[@]}" sheet --formulas "${tabs[@]}" --tab "New books=$DATA/sheet-new.csv"
  rm -f "$DATA"/sheet-*.csv "$DATA"/topic-*.toml
fi

if [ "${#failed[@]}" -gt 0 ]; then
  echo "${#failed[@]} step(s) failed:"
  printf '  - %s\n' "${failed[@]}"
  exit 1
fi
echo "All steps succeeded."
