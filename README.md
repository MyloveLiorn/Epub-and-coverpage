# govbooks

Find books published by US government offices on topics you choose, then check which ones are worth selling on Amazon and keep tracking them.

The pipeline has four steps:

1. **Map the offices.** Federal agencies and their hierarchy come from the [Federal Register API](https://www.federalregister.gov/developers/documentation/api/v1). State agencies for all 50 states come from [Wikidata](https://query.wikidata.org/). Both are stored as one tree, for example `Federal Government → Agriculture Department → Forest Service` or `State of California → California Natural Resources Agency → CAL FIRE`.
2. **Discover books.** Each topic keyword is searched in [GovInfo](https://api.govinfo.gov/docs/), the [Internet Archive](https://archive.org/advancedsearch.php), [Open Library](https://openlibrary.org/dev/docs/api/search) and [Google Books](https://developers.google.com/books). Only government publications are kept. Each one is tied to the most specific office in the map (so the Forest Service rather than the USDA), and one book found in several sources becomes one row.
3. **Check the Amazon market.** Each book is searched on Amazon and gets a verdict (for example `open_gap`: nobody sells it yet, but books on the topic sell) and a 0–100 score.
4. **Track.** Watched books and ASINs are snapshotted on every run. You're told when a competing edition appears, when the sales rank moves 20% or more, or when the price changes.

A second tool in this repo, [`govweb`](#govweb-the-government-website-map), maps federal and state government websites, searches them for books (for example army.mil and its sub-sites), and watches for new ones. It will be merged into `govbooks` later. The [weekly online run](#running-it-online-no-pc-needed) uses `govweb` only; the catalog search in step 2 is still available as a command.

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
| `catalog` (default) | nothing | no | Counts existing ISBN editions in Open Library (and Google Books when `GOOGLE_BOOKS_API_KEY` is set). A print book's ISBN-10 is its Amazon ASIN, so every edition links to its Amazon page. |
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
| `GOOGLE_BOOKS_API_KEY` | Optional. Book search in Google Books rate-limits quickly without it, and the `catalog` Amazon check uses Google Books only when it is set. |

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
3. **The crawl.** For each site, `govweb crawl` reads `robots.txt` and the sitemaps, then follows links from the home page. It crawls pages that look like publication listings first ("publications", "library", "reports", "handbooks" and so on). With `--topic`, `--all-topics` or `--keywords`, links and sitemap pages that mention a topic ("survival", "honey bees") come before everything else. It records every linked PDF, EPUB, MOBI and Word document, using its link text as the title, and scores how book-like each one is (handbooks and guides score up, agendas and forms score down).
4. **Sub-sites.** A crawl stays on one host. Other subdomains it finds are added to the map as sub-sites that inherit their parent's owner; army.mil alone has about 200. `--with-subsites` crawls them right after their parent, a batch per run (`--max-subsites`, default 30). Sub-sites whose names suggest a publisher (armypubs, history, armyupress, library) or a topic go first.
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
govweb crawl army.mil --with-subsites --all-topics --max-pages 400   # army.mil in depth, topics first
govweb crawl army.mil --subsites-only --max-subsites 40               # only the next 40 sub-sites
govweb crawl --level state --state CA --limit 40 --max-minutes 6      # California's sites, 6 minutes each at most
govweb docs --topic beekeeping --books-only --reusable
govweb new --days 7 --books-only               # what appeared on re-crawls this week
govweb amazon                                  # is each book already on Amazon?
govweb export --out books.csv                  # the results table (also: --new-only --days 7)
govweb export --out bees.csv --topics-only --config govbooks.toml   # only books on a topic
govweb ntrs "Apollo 13"                        # NASA's Technical Reports Server (open API, with PDFs)
govweb export --out a13.csv --sites nasa.gov --keywords "Apollo 13" --all-documents

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

## Running it online (no PC needed)

The workflow `.github/workflows/find-books.yml` runs `automation/run.sh` every Monday on GitHub's servers, and can also be started by hand. It searches government websites only; it doesn't search book catalogs such as the Internet Archive or Google Books. Each run:

1. Refreshes the map: the .gov registry and, in the first week of each month, the federal and state agency websites.
2. Searches the **priority sites** in `automation/priority-sites.txt` in depth (army.mil to start with): up to 400 pages each, then the next 40 of their sub-sites (armypubs.army.mil, history.army.mil, armyupress.army.mil, ...), 120 pages each. The priority sites are searched every week, so new books on them are caught quickly.
3. Searches **state websites** in depth: five states per run, in turn, so all 50 are covered every 10 weeks. For each state it searches up to 40 of its sites, 150 pages each. Sites never searched come first, and among them those whose names suggest books (library, archives, history, geology, publications).
4. Crawls the next 150 federal sites not crawled in the last 30 days, so the whole federal map is covered over a few weeks.
5. Every crawl follows links about the topics in `automation/govbooks.toml` first. Each site has a time limit, and pages of one shape (such as `Details.aspx?ID=1`, `?ID=2`, …) are dropped after five of them link to no documents.
6. Checks whether each book is already on Amazon, books on the topics first. Without keys it uses the free check (Open Library editions; each ISBN-10 is also the Amazon product number). It uses Keepa automatically once `KEEPA_API_KEY` is set.
7. Saves the results to the `data` branch:
   - `topic-books.csv`: books on your topics
   - `new-books.csv`: books that appeared on government websites this week
   - `website-books.csv`: every book found on the websites
   - `last-run.md`: the books found by this run, also shown on the run's page in **Actions**
   - `govweb.db.gz`: the database the next run continues from
8. Fills the Google Sheet, if one is set up.

Each results row gives the title, the matching topics, the publisher, level and state, the link, the copyright screening, and whether it's on Amazon (`yes`, `no`, `not found`, `not checked`, or `title too generic to check` for titles such as "2021 Annual Report") with a link. A title of fewer than four distinctive words only counts as found when the Amazon listing also names the publisher, since same-title books by others are common. The same book linked twice from one website is listed once.

**Start a run by hand:** open the repo on GitHub, then **Actions** → **Find government books** → **Run workflow**. For that run only, you can add more sites to search in depth (for example `navy.mil nps.gov`), choose the states to search (for example `CA TX`), and set how many sites to crawl.

**Search any site for a subject, on demand:** **Actions** → **Search sites** → **Run workflow**. Enter the sites (for example `nasa.gov history.nasa.gov`) and the words to look for (for example `Apollo 13, Apollo XIII`). It searches those sites and 20 of their sub-sites, following links about the words first, plus NASA's Technical Reports Server when a NASA site is chosen. It checks the books it finds on Amazon, and shows the table on the run's page (also downloadable as `books.csv`). It starts from the weekly run's database but doesn't change it, so it can run at any time. On a PC: `SITES="nasa.gov" KEYWORDS="Apollo 13" automation/search.sh`.

**Change the topics:** edit `automation/govbooks.toml`. **Change the priority sites:** edit `automation/priority-sites.txt` (one site per line). Both can be edited in the GitHub website or app.

**Keys** go under **Settings** → **Secrets and variables** → **Actions**. All are optional:

| Secret | What it adds |
|---|---|
| `KEEPA_API_KEY` | Real Amazon results with sales rank (paid) |
| `AMAZON_CREATORS_CREDENTIAL_ID`, `AMAZON_CREATORS_CREDENTIAL_SECRET`, `AMAZON_PARTNER_TAG` | Amazon Creators API instead of Keepa |
| `GOOGLE_BOOKS_API_KEY` | The free Amazon check also looks in Google Books |
| `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_SHEET_ID` | Writes the results into a Google Sheet |

**Google Sheet setup (once):**
1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **Google Sheets API**.
2. Create a **service account** and download a JSON key for it.
3. Create a Google Sheet and share it with the service account's email address as an **Editor**.
4. Add two secrets:
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: the whole contents of the JSON key file.
   - `GOOGLE_SHEET_ID`: the long id in the sheet's web address, between `/d/` and `/edit`.

Each run then replaces three tabs: **Topic books**, **New books** and **Website books**. Cells are written as plain text, so nothing from a website can run as a formula.

This repository is public, so the `data` branch is public too. It holds only public information about government books; keys stay private in the secrets.

The same steps work on a PC: `automation/run.sh` from the repository root, after `pip install -e ".[sheets]"`.

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
                   book-likeness), crawl.py, watch.py (new-book watches), amazon.py, export.py
                   (results table), sheets.py (Google Sheets), tree.py, db.py, cli.py
automation/        run.sh (the weekly job), govbooks.toml (its topics), priority-sites.txt (sites searched
                   in depth every week)
.github/workflows/ find-books.yml (runs the weekly job on GitHub)
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
