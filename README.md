# Signalpost agent — Norwegian company research with claim-level evidence

Give it a batch of Norwegian organisation numbers. For every input it returns exactly one terminal envelope with
legal identity, annual accounts, leadership, registered workplaces, **workforce stated in the official annual
accounts**, a verified official website and company-owned profiles, hiring signals and dated activity. Each fact
carries its source URL, retrieval time, content hash, claim span, reporting period where relevant, an explicit
availability state and — for external facts — a `signal_type` (`company_profile`, `profile_handle`,
`profile_metrics`, `public_post`, `job_posting`, `workforce_snapshot`, `review_summary`, `public_mention`).
Reruns diff against the previous snapshot.

No paid APIs, no LLM, no personal credentials: **expected third-party cost per official run is $0.**

## Run it (one command)

Python 3.12+. From a clean clone:

```bash
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt                        # declared install step (all versions pinned)
python -m signalpost --input companies.jsonl --output-dir out
```

The single evaluator command is the last line. `--input` accepts JSONL (`{"organisation_number": "..."}` per line),
a JSON list (or `{"organisation_numbers": [...]}`), CSV with an `organisation_number` column, or plain text with one
number per line. Every input row produces exactly one envelope, including duplicates and malformed numbers
(state `failed`, with the reason).

Outputs in `--output-dir`:

| File | Content |
|---|---|
| `envelopes.jsonl` | one terminal envelope per input, in input order |
| `run-report.json` | requests by source class, bytes, runtime, connector outcomes, website-identity outcomes, claim states, validation |
| `report.html` | self-contained viewer: search, filter, **screen** ("companies in Oslo with more than 5 employees top 10 by revenue"), **ask** per company, compare up to 4 companies; every fact links to its source (desktop + mobile) |

Research agent over the same output (cited answers, inspectable screen plans, abstention on unsupported questions):

```bash
python -m signalpost.ask --envelopes out/envelopes.jsonl --org 979543883 --question "Who runs it and how are the latest numbers?"
python -m signalpost.ask --envelopes out/envelopes.jsonl --screen "companies with revenue above 25 million top 5 by revenue"
```

Refresh history is kept in `--state-dir` (default `state/`). Run the same command again to get typed changes;
`--previous old/envelopes.jsonl` diffs against an earlier output file instead.

### Budgets and options

| Option | Default | Env var |
|---|---|---|
| `--max-requests` | 18 × companies (published limit: 2,000 per 100 companies; 10% margin kept) | `SIGNALPOST_MAX_REQUESTS` |
| `--requests-per-company` | 18 | `SIGNALPOST_REQUESTS_PER_COMPANY` |
| `--time-budget-min` | 45 minutes; optional work stops ~12% before the deadline, then outputs are finalized | `SIGNALPOST_TIME_BUDGET_MIN` |
| `--workers` | 16 network threads | `SIGNALPOST_WORKERS` |
| `--ocr-workers` | CPU count − 2 (max 12) OCR processes (~110 MB each) | `SIGNALPOST_OCR_WORKERS` |
| `--workforce-share` | 0.7 of the non-mandatory request budget may go to annual-account copies (also capped by OCR time) | `SIGNALPOST_WORKFORCE_SHARE` |
| OCR backend | Tesseract CLI if installed, else RapidOCR (pip); `none` disables | `SIGNALPOST_OCR_BACKEND` |
| `--registry-dir` / `--bulk` | evaluator-supplied Brønnøysund bulk files / single entity snapshot (starter-kit flag) | `SIGNALPOST_REGISTRY_DIR` / `SIGNALPOST_BULK` |
| `--jobs-days` | 40 days of the NAV job feed | `SIGNALPOST_JOBS_DAYS` |

Requests are reserved per phase: registry bulk (3) → accounts (1 per company) → annual-account copies (OCR
capacity × share) → NAV feed, Wikidata, directory sitemaps (~100 for the whole batch) → websites. When budget or
time runs out the remaining work is reported as `blocked`, never dropped. Provisional envelopes are written right
after the registry phase, so even a hard kill leaves one row per input.

## How it works

```
batch ─► Brønnøysund bulk (entities, roles, workplaces: 3 requests for the whole batch)
      ├─► [background processes] annual-account copy PDF (orgnr-keyed official URL) → OCR → "Antall årsverk"
      ─► Regnskapsregisteret (1 request/company → last 3 filed years, reporting period on every value)
      ─► NAV job feed (official; ads confirmed by employer.orgnr = company or one of its workplaces)
      ─► Wikidata (1 SPARQL request: items with P2333 = organisation number) · Fagfolkguiden sitemap index
      ─► website candidates: registry hjemmeside → registry e-mail domain → workplace site/e-mail → NAV employer
         homepage → Wikidata P856 → directory listing → domains derived from the legal name
      ─► identity gate (below) ─► static crawl: home, contact/about, careers, news, people, RSS, sitemap
      ─► claims + evidence ─► refresh diff ─► summary ─► envelope
```

### Workforce from the official annual accounts

Norwegian annual accounts disclose the number of full-time equivalents (årsverk). Brønnøysund serves each filing as a
scanned PDF at `regnskapsregisteret/regnskap/aarsregnskap/kopi/{orgnr}/{year}`, so identity is exact by construction.
Pages are rendered with pdfium and read with OCR in the order where the payroll note usually sits (pages 5, 4, 6, 3…),
stopping at the first explicit company-level statement. The claim quotes the statement as its evidence span with the
page number and PDF hash. Group/subsidiary wording, conflicting statements and OCR-ambiguous numbers (e.g. "100"
where the template prints "1.00") abstain. On random companies the statement is found for ~85–90 % of filers.

### Exact-identity gate (precision first)

* `exact` — this organisation number appears on the site (and the site is named for the company or shows no other number).
* `strong` — an official/open source declares the site (registry, NAV ad, Wikidata) **and** the legal name/domain match;
  or a registry e-mail domain used only by this organisation plus name and phone/address/registered-person corroboration.
  Name-derived domains need domain + legal name + a registry phone/address/role-holder match on the site.
* `registry_declared` — the registry lists the site for this organisation only and nothing contradicts it: only the URL
  is published, none of the site's content.
* `shared_site` — the domain is registered for several organisations (housing-cooperative managers, accountants,
  groups, franchises). Published only as `shared_website`; none of its content is attributed to the company.
* `conflict` / `ambiguous` — a different organisation number is shown, or proof is insufficient: nothing is published.

### Publication rules (v4, after Builderr's audits of 23 Oct and 1 Nov)

* **Own site only — otherwise blank with a reason.** A website's content is attributed only when the site is proven to be
  the company's own: this organisation number on the site, the full legal name in the site title, or a name-matching
  domain together with the full legal name on the site. A site that could belong to a parent group or sister company
  (several other organisation numbers, a foreign parent domain, group branding, an "our companies" page, or a domain that
  matches only part of the name, e.g. nilsson.no for NILSSON HARAS AS) is left blank: `official_website` is `ambiguous`
  with the `candidate_url` and a one-line reason, and nothing from that site is published.
* **Social links.** Kept only when the handle carries the company's own name, brand or domain. Numeric IDs,
  theme/site-builder/agency handles and sister-company links are dropped.
* **News — the article itself is checked.** Every candidate article is fetched; it is kept only if it is not a product
  page, has a genuine publication date (JSON-LD `datePublished`, `article:published_time`, or a `<time>` inside the
  article — never a feed "updated", sitemap "lastmod" or render date), and its headline or body names the company
  (legal name or its own verified brand; footer and browser-title mentions do not count).
* **Jobs.** Only NAV ads confirmed by organisation number, or a structured `JobPosting` whose hiring organisation names
  the company. A group careers page is not this company's.
* **Accounts.** "Latest accounts" is the newest filed period of the company's own accounts (cross-checked against the
  registry's latest submitted year) with an explicit fiscal-year label. Company (`SELSKAP`) and group (`KONSERN`)
  accounts are never mixed; growth is computed only against the immediately preceding period of equal length,
  written older → newer.
* **Confidence on every link.** Each published fact carries a `confidence`; the viewer shows it next to every fact, and
  every withheld item shows why.

## Envelope

```jsonc
{
  "organisation_number": "979543883", "legal_name": "DIPS AS", "state": "available",
  "run": {"run_id": "...", "started_at": "...", "completed_at": "...", "terminal_status": "completed"},
  "summary": {"statements": [{"topic": "finances", "text": "...", "evidence_ids": ["ev-..."]}], "unknowns": ["..."]},
  "sections": {                                   // the seven required sections with a state each
    "legal_identity_and_brand": {"state": "available"}, "annual_accounts": {...}, "leadership_and_workplaces": {...},
    "website_and_profiles": {...}, "hiring_and_activity": {...}, "claim_evidence": {...}, "refresh": {...}},
  "claims": [{"key": "workforce.2025", "section": "hiring_and_activity", "field": "workforce", "signal_type": "workforce_snapshot",
              "external": true, "value": {"value": 3, "measure": "full_time_equivalents", "year": "2025"},
              "reporting_period": {"start": "2025-01-01", "end": "2025-12-31"}, "availability": "available",
              "confidence": 0.95, "evidence_ids": ["ev-..."], "first_seen": "...", "last_seen": "..."}],
  "evidence": [{"id": "ev-...", "source_url": "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/<org>/2025",
                "source_class": "official_annual_account_copy", "retrieved_at": "...", "content_sha256": "...",
                "claim_span": "antall arsverk i regnskapsaret 3.00", "extraction_method": "pdf_ocr_rapidocr", "page": 5}],
  "changes": [{"change_type": "new_role", "material": true, "previous_value": null, "current_value": {...}}],
  "website_identity": {"candidate": "dips.com", "origin": "registry_website", "identity": "exact", "proofs": [...], "pages": [...]},
  "errors": [], "operations": {"third_party_cost_usd": 0}
}
```

Availability states: `available`, `not_available` (looked, nothing there), `blocked` (robots/HTTP refusal or budget),
`not_applicable`, `ambiguous` (could not tie to this exact entity), `failed` (source error). Missing values are never
zero-filled; a workforce of 0 is only published when the filed accounts state it, with the quoted statement.

## Sources and rights

| Source | Use | Access |
|---|---|---|
| Brønnøysundregistrene Enhetsregisteret bulk (entities, roles, workplaces) and API fallback | identity, roles, workplaces, contact data, dated registry events | open data, NLOD 2.0; birth dates discarded |
| Regnskapsregisteret API and annual-account copies | three latest filed years of accounts; workforce statement (OCR of the latest filing copy) | open official data |
| NAV Job Vacancy Feed `pam-stilling-feed.nav.no` | active job ads with employer organisation number | free; NAV's published public token, fetched at run time; NAV terms of use |
| Wikidata SPARQL | items stating the organisation number (P2333): website, profiles, employees, Wikipedia article | CC0 open data, one request per batch |
| Fagfolkguiden `/bedrift/<slug>-<orgnr>` | directory profile, displayed aggregate rating | public pages allowed by robots.txt; discovered via the site's own sitemaps |
| Company-owned websites | brand, description, profiles, contact, jobs, news | static HTML only, robots.txt honoured, ≤0.5 s/page per host, identifying User-Agent |

Not used: search-result scraping, LinkedIn/Meta/Glassdoor/Indeed scraping, paid or personal API keys.
Outbound URL policy: http(s) only, no credentials in URLs, private/loopback/link-local targets refused (also after redirects),
2.5 MB page cap (25 MB for filing PDFs). No secrets are required or stored.

**Models / APIs / licences:** no LLM and no paid API. OCR runs locally: RapidOCR (Apache-2.0) with PP-OCR ONNX models on
onnxruntime (MIT), pdfium via pypdfium2 (Apache-2.0/BSD-3), OpenCV (Apache-2.0); Tesseract (Apache-2.0) when installed.
Other dependencies: requests (Apache-2.0), beautifulsoup4 (MIT). All pinned in `requirements.txt`.
**Expected cost per official run: $0.00.**

## Smoke test and measured runs

`smoke-test/` was produced from a **fresh clone**: new folder, empty virtual environment, only `pip install -r
requirements.txt`, empty cache (registry downloaded during the run), then the run command. `smoke-test/refresh-rerun/`
is the same batch run again against the stored state. The `request_budget` section of each `run-report.json` gives
the total request count and requests per 100 companies. Offline regression tests: `python -m unittest discover -s tests`.

| Run (16-thread laptop, RapidOCR) | Envelopes | Requests | Per 100 companies | Wall time | Workforce statements |
|---|---|---|---|---|---|
| smoke-test/ — fresh clone, empty cache | 100/100 | 625 | 625 (limit 2,000) | 14.7 min | 95 |
| smoke-test/refresh-rerun/ | 100/100, **0 changes** | 617 | 617 | 3.9 min | 95 |

Request use per 100 companies: 3 registry bulk files for the batch, ~100 entity rows (only if the bulk file is
unavailable), 200 live role/workplace calls, 100 accounts calls, 100 filing copies, ~80 NAV feed pages for the batch,
and website pages for the companies that have one. Annual-account copies are fetched at most one every 2.1 s
(Brønnøysund answers HTTP 429 above ~30/min) with one retry; OCR results are cached by PDF content fingerprint so an
unchanged filing is not re-read on refresh. Bulk downloads resume with HTTP Range after a dropped connection.

## Limitations

* Most Norwegian entities are small holding/property companies with no web presence; they get full registry,
  accounts and workforce coverage and explicit `not_available` web states.
* OCR throughput bounds the workforce family on very large batches: filings not processed before the soft deadline are
  reported as `blocked`. If neither Tesseract nor RapidOCR can be imported the family is reported as `blocked`.
* Websites that are neither listed in official/open sources nor derivable from the name are not discovered (no search API).
* NAV covers most, not all, Norwegian job ads (no FINN.no ads); the feed window is 40 days by default.
* JavaScript-only sites are fetched as static HTML; their content may be thin.
