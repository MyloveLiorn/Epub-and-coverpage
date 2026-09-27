# govbooks

Find books published by US government offices on topics you choose, then check which ones are worth selling on Amazon and keep tracking them.

The pipeline has four steps:

1. **Map the offices.** Federal agencies and their hierarchy come from the [Federal Register API](https://www.federalregister.gov/developers/documentation/api/v1). State agencies for all 50 states come from [Wikidata](https://query.wikidata.org/). Both are stored as one tree, for example `Federal Government → Agriculture Department → Forest Service` or `State of California → California Natural Resources Agency → CAL FIRE`.
2. **Discover books.** Each topic keyword is searched in [GovInfo](https://api.govinfo.gov/docs/), the [Internet Archive](https://archive.org/advancedsearch.php), [Open Library](https://openlibrary.org/dev/docs/api/search) and [Google Books](https://developers.google.com/books). Only government publications are kept. Each one is tied to the most specific office in the map (so the Forest Service rather than the USDA), and one book found in several sources becomes one row.
3. **Check the Amazon market.** Each book is searched on Amazon and gets a verdict (for example `open_gap`: nobody sells it yet, but books on the topic sell) and a 0–100 score.
4. **Track.** Watched books and ASINs are snapshotted on every run. You're told when a competing edition appears, when the sales rank moves 20% or more, or when the price changes.

A second tool in this repo, [`govweb`](#govweb-the-government-website-map), maps federal and state government websites, finds the books they host, and watches for new ones. It will be merged into `govbooks` later.

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

Both tools screen every work with the same rules (`govbooks/copyright_policy.py`), and give it a `rights` value:

| Value | Meaning |
|---|---|
| `public_domain` | Published 95 or more years ago, or the source says it's public domain. |
| `likely_public_domain` | A US federal work ([17 U.S.C. §105](https://www.law.cornell.edu/uscode/text/17/105)), or a work from a state whose law makes its works free to reuse. |
| `check` | Depends on the item: federal exceptions, states with mixed or unclear rules, local and tribal governments, university extension publications. |
| `likely_copyrighted` | A work from a state that claims copyright in its publications. Ask the state for permission. |
| `unknown` | Government authorship not confirmed. |

**Federal exceptions.** Even federal works are not always free:
- **Contractors and grantees:** works they write, and third-party photos or text inside federal books, can be copyrighted.
- **US Postal Service:** its works are outside §105, and stamp designs after 1978 are copyrighted.
- **Smithsonian:** works by its trust-fund staff and contractors can be copyrighted.
- **Federal Reserve Banks:** they are not federal agencies, so their publications are copyrighted.
- **NIST Standard Reference Data:** it can be copyrighted.
- **Outside the US:** the US may claim copyright abroad, so check before choosing worldwide sales territories.

**States.** `govbooks/data/state_copyright.json` records each state's rule, with the statutes, cases or official statements behind it, sources and a confidence level. It was researched in September 2026, mainly from Harvard's [State Copyright Resource Center](https://copyright.lib.harvard.edu/states/), state statutes and attorney general opinions. States without clear evidence are marked `unclear` rather than guessed.

| Rule | States | Screened as |
|---|---|---|
| `public_domain` | California and Florida (high confidence); North Carolina (medium) | `likely_public_domain` when high confidence, otherwise `check` |
| `claims_copyright` | 24 states, e.g. Colorado, Michigan, Nevada, Pennsylvania | `likely_copyrighted` |
| `mixed` | Illinois, Massachusetts, Minnesota, Texas | `check` |
| `unclear` | 19 states | `check` |

See the whole table with `govweb copyright`, or one state with `govweb copyright --state TX`. Twenty states rest on low-confidence evidence (agency website terms, library statements), so re-check the sources before relying on them. Two things hold in every state: laws and court opinions themselves are free to use, and even in California and Florida, work by contractors or third parties can still be copyrighted.

`market check` and `report` include only the first two values unless you pass `--any-rights`; `govweb docs --reusable` does the same. This is a screening aid, not legal advice: read each item's own rights notice. Also, Kindle Direct Publishing accepts public-domain books only when you add something new (annotations, translation, illustrations, etc.). Read KDP's current public-domain content guidelines before publishing.

## govweb: the government website map

`govweb` maps US federal and state government websites, finds the books hosted on them, and tells you when new ones appear. It is separate from `govbooks` for now, with its own database (`govweb.db`). The two already share topics (`--topic` reads them from `govbooks.toml`) and copyright rules.

1. **The map.** The [official .gov registry](https://github.com/cisagov/dotgov-data), published by CISA, lists about 16,800 domains with their type (federal, state, county, city, tribal, special district and more), owning organization and state. `govweb sync` loads it. The tree groups federal, interstate and tribal sites by organization, and all other levels by state first.
2. **Agency websites.** `govweb agencies sync` adds every federal agency (Federal Register) and state agency (Wikidata) with an official website. That fills two gaps in the registry:
   - Agencies on subdomains, such as `fs.usda.gov` (Forest Service) or `water.ca.gov` (California Department of Water Resources).
   - Agencies outside `.gov`, such as `army.mil` or `dot.state.tx.us`.
3. **The crawl.** For each site, `govweb crawl` reads `robots.txt` and the sitemaps, then follows links from the home page. It crawls pages that look like publication listings first ("publications", "library", "reports", "handbooks" and so on). It records every linked PDF, EPUB, MOBI and Word document, using its link text as the title, and scores how book-like each one is (handbooks and guides score up, agendas and forms score down).
4. **Sub-sites.** A crawl stays on one host. Other subdomains it finds are added to the map as sub-sites that inherit their parent's owner, and each can be crawled on its own.
5. **Any website.** `govweb crawl https://any.site/` works on any site, not only government ones. Name it with `govweb add`.
6. **New books.** Re-crawls start from the pages where documents were found before. Documents that appear on a later crawl count as new; everything found on a site's first crawl is the baseline. A **watch** is a saved search (a topic or keywords, plus a level, state or site). `govweb watch run` re-crawls the sites due and reports only the new matching books.
7. **Copyright.** Every document is screened by who published it: the federal rule and its exceptions, or its state's policy (see [Copyright screening](#copyright-screening)).

```bash
govweb sync                                    # load the .gov registry
govweb agencies sync                           # add federal and state agency websites
govweb map --level state --state CA            # California's sites, by organization
govweb map --level federal --out federal.json  # the tree as JSON
govweb crawl --level federal --limit 50        # the first 50 federal sites
govweb crawl --state CA --level state          # California state sites
govweb crawl https://www.army.mil              # any site
govweb docs --topic beekeeping --books-only --reusable
govweb new --days 7 --books-only               # what appeared on re-crawls this week

govweb watch add bees --topic beekeeping --level federal
govweb watch add ca-guides --keywords "field guide, handbook" --state CA
govweb watch run --out reports/new-books.md    # e.g. weekly, from Task Scheduler or cron
govweb copyright                               # the policy table; --state TX for details
```

**Politeness:**
- It identifies itself as `govweb/0.1` and obeys `robots.txt`, including crawl delays up to 10 seconds.
- It waits at least 1 second between requests to the same host (`--delay`).
- It fetches at most 100 pages per site (`--max-pages`; 60 for watch re-crawls) and only reads HTML pages, capped at 2 MB each. Documents are recorded but not downloaded.
- Sites run in parallel (`--workers`, default 4), but each individual site is crawled one request at a time.
- `crawl` skips sites crawled in the last 30 days (`--skip-recent-days`). `watch run` re-crawls sites last crawled over 7 days ago (`--recrawl-days`).

At the defaults, a site takes 2 to 3 minutes. All 1,317 federal domains take about 12 hours with 4 workers.

The map holds no personal data: the registry's security-contact email column is dropped on import.

## Development

```bash
pip install -e '.[dev]'
pytest
```

The tests use recorded response shapes for every API, so they run offline.

## Layout

```
govweb/            the government website map: registry.py (.gov list), agencies.py (agency websites),
                   fetch.py (polite fetching), parse.py (HTML and sitemaps), classify.py (documents,
                   book-likeness), crawl.py, watch.py (new-book watches), tree.py, db.py, cli.py
govbooks/
  copyright_policy.py  federal rule and exceptions, state policies (data/state_copyright.json)
  agencies/        federal_register.py, wikidata.py, index.py (name → office matching), tree helpers
  sources/         govinfo.py, internet_archive.py, open_library.py, google_books.py
  market/          base.py (scoring), keepa.py, creators.py, catalog.py
  discover.py      topic scoring, copyright screening, merging records into books
  tracking.py      watchlist snapshots and change detection
  report.py        Markdown / CSV export
  db.py            SQLite schema and queries
  cli.py           the govbooks command
```
