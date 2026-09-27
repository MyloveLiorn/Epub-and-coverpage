#!/usr/bin/env bash
# The weekly job: map the sites, crawl a batch of them, search the book catalogs, check Amazon,
# and write the results tables. Run from the repository root. Every step runs even if an
# earlier one fails; the script exits non-zero at the end if any step failed.
set -uo pipefail

DATA="${DATA_DIR:-data}"
CONFIG="${GOVBOOKS_CONFIG:-automation/govbooks.toml}"
FEDERAL_SITES="${FEDERAL_SITES:-150}"
STATE_SITES="${STATE_SITES:-150}"
AMAZON_CHECKS="${AMAZON_CHECKS:-300}"
if [ -z "${AMAZON_PROVIDER:-}" ]; then
  if [ -n "${KEEPA_API_KEY:-}" ]; then AMAZON_PROVIDER=keepa; else AMAZON_PROVIDER=catalog; fi
fi

mkdir -p "$DATA"
failed=()
step() {
  echo "::group::$*"
  if ! "$@"; then
    echo "::warning::Step failed: $*"
    failed+=("$*")
  fi
  echo "::endgroup::"
}

web=(govweb --db "$DATA/govweb.db")
books=(govbooks --config "$CONFIG" --db "$DATA/govbooks.db")

# 1. The map: the .gov registry and agency websites.
step "${web[@]}" sync
step "${web[@]}" agencies sync

# 2. Crawl the next batch of federal and state sites (sites crawled in the last 30 days wait).
step "${web[@]}" crawl --level federal --limit "$FEDERAL_SITES" --max-pages 60 --workers 8
step "${web[@]}" crawl --level state --limit "$STATE_SITES" --max-pages 60 --workers 8

# 3. Search the book catalogs (GovInfo, Internet Archive, Open Library, Google Books) per topic.
step "${books[@]}" agencies sync
topics=$(python -c "import sys, tomllib; print(' '.join(tomllib.load(open(sys.argv[1], 'rb'))['topics']))" "$CONFIG")
for topic in $topics; do
  step "${books[@]}" discover "$topic"
  step "${books[@]}" market check --topic "$topic" --provider "$AMAZON_PROVIDER" --any-rights --limit 100
done

# 4. Amazon check for books found on websites, then the results tables.
step "${web[@]}" amazon --provider "$AMAZON_PROVIDER" --limit "$AMAZON_CHECKS" --config "$CONFIG"
step "${web[@]}" export --out "$DATA/new-books.csv" --new-only --days 8
step "${web[@]}" export --out "$DATA/website-books.csv"
step "${books[@]}" report --any-rights --out "$DATA/catalog-books.csv"
step "${web[@]}" stats

# 5. Google Sheet, when its secrets are set.
if [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON:-}" ] && [ -n "${GOOGLE_SHEET_ID:-}" ]; then
  step "${web[@]}" sheet \
    --tab "New books=$DATA/new-books.csv" \
    --tab "Website books=$DATA/website-books.csv" \
    --tab "Catalog books=$DATA/catalog-books.csv"
fi

if [ "${#failed[@]}" -gt 0 ]; then
  echo "${#failed[@]} step(s) failed:"
  printf '  - %s\n' "${failed[@]}"
  exit 1
fi
echo "All steps succeeded."
