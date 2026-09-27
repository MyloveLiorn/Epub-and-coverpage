# govbooks

Find books published by US government offices on topics you choose, then check which ones are worth selling on Amazon and keep tracking them.

The pipeline has four steps:

1. **Map the offices.** Federal agencies and their hierarchy come from the [Federal Register API](https://www.federalregister.gov/developers/documentation/api/v1). State agencies for all 50 states come from [Wikidata](https://query.wikidata.org/). Both are stored as one tree, for example `Federal Government → Agriculture Department → Forest Service` or `State of California → California Natural Resources Agency → CAL FIRE`.
2. **Discover books.** Each topic keyword is searched in [GovInfo](https://api.govinfo.gov/docs/), the [Internet Archive](https://archive.org/advancedsearch.php), [Open Library](https://openlibrary.org/dev/docs/api/search) and [Google Books](https://developers.google.com/books). Only government publications are kept. Each one is tied to the most specific office in the map (so the Forest Service rather than the USDA), and one book found in several sources becomes one row.
3. **Check the Amazon market.** Each book is searched on Amazon and gets a verdict (for example `open_gap`: nobody sells it yet, but books on the topic sell) and a 0–100 score.
4. **Track.** Watched books and ASINs are snapshotted on every run. You're told when a competing edition appears, when the sales rank moves 20% or more, or when the price changes.

## Install

```bash
pip install -e .            # Python 3.11+, only depends on requests
govbooks init               # writes govbooks.toml with example topics
```

## Quick start

```bash
govbooks agencies sync                       # all federal agencies + all 50 states (about 5 minutes)
govbooks agencies tree --level federal --depth 1
govbooks discover beekeeping                 # topic names come from govbooks.toml
govbooks market check --topic beekeeping --watch-top 10
govbooks report --topic beekeeping --out reports/beekeeping.md
govbooks track run                           # run daily or weekly, e.g. from cron
```

To run all of it at once: `govbooks run beekeeping --watch-top 10`.

## Topics

Topics go in `govbooks.toml`:

```toml
[topics.food_preservation]
keywords = ["home canning", "food preservation", "canning", "pickling"]
exclude = ["canning industry"]   # any hit drops the book
min_score = 3                    # keyword in title = 3, in subjects = 2, in description = 1
```

## Walking the office tree

By default, `discover` searches for anything from the government publishers (the Government Printing/Publishing Office). To search office by office, name the offices:

```bash
govbooks agencies list --search "fish and wildlife"      # find ids
govbooks discover beekeeping --agency fr:12              # one agency, by id
govbooks discover beekeeping --under fr:12               # an agency and everything under it
govbooks discover beekeeping --state CA --max-agencies 50  # every California agency
govbooks agencies export --out map.json                  # the whole tree as nested JSON (or .csv)
```

State publications are mostly reached this way, because state offices rarely publish through the GPO.

## Amazon providers

Choose a provider with `[amazon] provider = ...` in the config or `--provider`:

| Provider | Needs | Sales rank | Notes |
|---|---|---|---|
| `catalog` (default) | nothing | no | Counts existing ISBN editions in Google Books and Open Library. A print book's ISBN-10 is its Amazon ASIN, so every edition links to its Amazon page. |
| `keepa` | `KEEPA_API_KEY` (paid) | yes | [Keepa](https://keepa.com/#!api) data: sales rank and price, the best fit for tracking. |
| `creators` | `AMAZON_CREATORS_CREDENTIAL_ID`, `AMAZON_CREATORS_CREDENTIAL_SECRET`, `AMAZON_PARTNER_TAG` | yes | Amazon's [Creators API](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/introduction), which replaced PA-API 5.0 in 2026. Requires an Amazon Associates account and version 3.x credentials (2.x credentials stopped working on 11 September 2026). |

Nothing here scrapes Amazon's website; that's against Amazon's terms.

**Verdicts** (`govbooks market verdicts`):

| Verdict | Meaning |
|---|---|
| `open_gap` | No edition on Amazon, and books on this topic sell. |
| `untested_gap` | No edition on Amazon; topic demand unknown or weak. |
| `proven_demand` | A few editions exist and at least one sells well (rank ≤ 300,000). |
| `low_demand` | A few editions exist but none sells well. |
| `some_competition` | A few editions exist (sales rank not available from this provider). |
| `crowded` | Four or more editions already on Amazon. |

The **score** combines demand (up to 60 points, from the best sales rank, or the topic's median rank when nothing matches) with competition (up to 40 points; fewer matching editions score higher). It is then scaled by how clear the copyright status is.

## Other keys

| Variable | Needed for |
|---|---|
| `GOVINFO_API_KEY` | GovInfo. It's free at [api.data.gov](https://api.data.gov/signup/), and `DEMO_KEY` is used if unset. |
| `GOOGLE_BOOKS_API_KEY` | Optional, but Google Books rate-limits quickly without it. |

## Copyright screening

Each book gets a `rights` value:

- `public_domain`: published 95 or more years ago, or the source says it's public domain.
- `likely_public_domain`: a US federal government work ([17 U.S.C. §105](https://www.law.cornell.edu/uscode/text/17/105)). These can still contain copyrighted material written by contractors or reproduced from others.
- `check`: a state government work. States set their own copyright policies.
- `unknown`: government authorship not confirmed.

`market check` and `report` include only the first two unless you pass `--any-rights`. This is a screening aid, not legal advice. Also, Kindle Direct Publishing accepts public-domain books only when you add something new (annotations, translation, illustrations, etc.). Read KDP's current public-domain content guidelines before publishing.

## Development

```bash
pip install -e '.[dev]'
pytest
```

The tests use recorded response shapes for every API, so they run offline.

## Layout

```
govbooks/
  agencies/        federal_register.py, wikidata.py, index.py (name → office matching), tree helpers
  sources/         govinfo.py, internet_archive.py, open_library.py, google_books.py
  market/          base.py (scoring), keepa.py, creators.py, catalog.py
  discover.py      topic scoring, copyright screening, merging records into books
  tracking.py      watchlist snapshots and change detection
  report.py        Markdown / CSV export
  db.py            SQLite schema and queries
  cli.py           the govbooks command
```
