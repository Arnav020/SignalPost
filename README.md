# Signalpost agent — Norwegian company research with claim-level evidence

Give it a batch of Norwegian organisation numbers. For every input it returns exactly one terminal envelope with
legal identity, annual accounts, leadership, registered workplaces, a verified official website and company-owned
profiles, hiring signals and dated activity. Each fact carries its source URL, retrieval time, content hash, claim span,
reporting period where relevant, and an explicit availability state. Reruns diff against the previous snapshot.

No paid APIs, no LLM, no personal credentials: **expected third-party cost per official run is $0.**

## Run it (one command)

Python 3.12+. From a clean clone:

```bash
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt                        # declared install step (pinned)
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
| `run-report.json` | requests by source class, bytes, runtime, website-identity outcomes, claim-state counts, validation |
| `report.html` | self-contained viewer: search, filter, compare up to 4 companies, every fact links to its source (desktop + mobile) |

Refresh history is kept in `--state-dir` (default `state/`). Run the same command again to get typed changes;
`--previous old/envelopes.jsonl` diffs against an earlier output file instead.

### Budgets (defaults match the published daily limits)

| Option | Default | Env var |
|---|---|---|
| `--max-requests` | 2000 outbound HTTP requests per run | `SIGNALPOST_MAX_REQUESTS` |
| `--time-budget-min` | 45 minutes; optional work stops ~12% before the deadline, then outputs are finalized | `SIGNALPOST_TIME_BUDGET_MIN` |
| `--workers` | 16 | `SIGNALPOST_WORKERS` |
| `--registry-dir` | evaluator-supplied Brønnøysund bulk files (names containing `enhet`, `underenhet`, `roll`) instead of downloading | `SIGNALPOST_REGISTRY_DIR` |
| `--bulk` | a single Brreg entity snapshot file (same flag as the starter kit, e.g. `brreg-enheter.csv`) | `SIGNALPOST_BULK` |
| `--jobs-days` | 40 days of the NAV job feed | `SIGNALPOST_JOBS_DAYS` |

Request spending is ordered by value: 3 registry bulk files → 1 accounts call per company (reserved up front) →
NAV job feed → registry-declared websites → name-derived domains. When the budget or time runs out the remaining
work is reported as `blocked`, never dropped. Provisional envelopes are written right after the registry phase,
so even a hard kill leaves one row per input.

## How it works

```
batch ─► Brønnøysund bulk (entities, roles, workplaces: 3 requests for the whole batch)
      ─► Regnskapsregisteret (1 request/company → last 3 filed years, reporting period on every value)
      ─► NAV job feed (official; ads confirmed by employer.orgnr = company or one of its workplaces)
      ─► website candidates: registry hjemmeside → registry e-mail domain → workplace site/e-mail
                             → employer homepage in a confirmed NAV ad → domains derived from the legal name
      ─► identity gate (below) ─► static crawl: home, contact/about, careers, news, people, RSS, sitemap
      ─► claims + evidence ─► refresh diff ─► summary ─► envelope
```

### Exact-identity gate (precision first)

* `exact` — this organisation number appears on the site (and the site is named for the company or shows no other number).
* `strong` — official source declares the site (registry or NAV ad) **and** the legal name/domain match; or a registry
  e-mail domain that only this organisation uses plus name and phone/address/registered-person corroboration.
  Name-derived domains need domain + legal name + a registry phone/address/role-holder match on the site.
* `registry_declared` — the registry lists the site for this organisation only, nothing contradicts it.
* `shared_site` — the domain is registered for several organisations (housing-cooperative managers, accountants,
  groups, franchises). Published only as `shared_website`; none of its content is attributed to the company.
* `conflict` / `ambiguous` — a different organisation number is shown, or proof is insufficient: nothing is published.

Social profiles count only when linked from the verified site; with several profiles on one platform (partner,
customer or sister-company links), only a unique best match on the legal name survives.

## Envelope

```jsonc
{
  "organisation_number": "979543883", "legal_name": "DIPS AS", "state": "available",
  "run": {"run_id": "...", "started_at": "...", "completed_at": "...", "terminal_status": "completed"},
  "summary": {"statements": [{"topic": "finances", "text": "...", "evidence_ids": ["ev-..."]}], "unknowns": ["..."]},
  "sections": {                                   // the seven required sections with a state each
    "legal_identity_and_brand": {"state": "available"}, "annual_accounts": {...}, "leadership_and_workplaces": {...},
    "website_and_profiles": {...}, "hiring_and_activity": {...}, "claim_evidence": {...}, "refresh": {...}},
  "claims": [{"key": "accounts.2025.revenue", "section": "annual_accounts", "field": "revenue", "value": 790400865.0,
              "unit": "NOK", "reporting_period": {"start": "2025-01-01", "end": "2025-12-31"},
              "availability": "available", "confidence": 1.0, "evidence_ids": ["ev-..."], "first_seen": "...", "last_seen": "..."}],
  "evidence": [{"id": "ev-...", "source_url": "https://data.brreg.no/regnskapsregisteret/regnskap/979543883",
                "source_class": "official_annual_accounts", "retrieved_at": "...", "content_sha256": "...",
                "claim_span": "regnskapsperiode 2025-01-01..2025-12-31; ...", "extraction_method": "regnskapsregisteret_json"}],
  "changes": [{"change_type": "new_role", "material": true, "previous_value": null, "current_value": {...}}],
  "website_identity": {"candidate": "dips.com", "origin": "registry_website", "identity": "exact", "proofs": [...], "pages": [...]},
  "errors": [], "operations": {"third_party_cost_usd": 0}
}
```

Availability states: `available`, `not_available` (looked, nothing there), `blocked` (robots/HTTP refusal or budget),
`not_applicable`, `ambiguous` (could not tie to this exact entity), `failed` (source error). Missing values are never
zero-filled; absent accounts lines are simply not emitted.

## Sources and rights

| Source | Use | Access |
|---|---|---|
| Brønnøysundregistrene Enhetsregisteret bulk (entities, roles, workplaces) and API fallback | identity, roles, workplaces, contact data, dated registry events | open data, NLOD 2.0; birth dates discarded |
| Regnskapsregisteret API | three latest filed years of accounts, filing-copy links | open data |
| NAV Job Vacancy Feed `pam-stilling-feed.nav.no` | active job ads with employer organisation number | free; NAV's published public token, fetched at run time; NAV terms of use |
| Company-owned websites | brand, description, profiles, contact, jobs, news | static HTML only, robots.txt honoured, ≤0.5 s/page per host, identifying User-Agent |

Not used: search-result scraping, LinkedIn/Meta/Glassdoor/Indeed scraping, paid or personal API keys.
Outbound URL policy: http(s) only, no credentials in URLs, private/loopback/link-local targets refused (also after redirects),
2.5 MB page cap. No secrets are required or stored.

**Models / APIs / licences:** no LLM and no paid API. Python dependencies are pinned in `requirements.txt`
(requests — Apache-2.0, beautifulsoup4 — MIT). **Expected cost per official run: $0.00.**

## Smoke test and measured runs

`smoke-test/` holds the 100-company smoke test (`companies-100.jsonl` input, `envelopes.jsonl`, `run-report.json`,
`report.html`, `run-1.log`): **100 inputs → 100 terminal envelopes**, 331 requests, $0. `smoke-test/refresh-rerun/` is the
same batch run again against the stored state: 100 envelopes, **0 changes** (idempotent refresh).
Offline regression tests: `python -m unittest discover -s tests`.

Local measurements (consumer connection):

| Run | Envelopes | Requests | Wall time | Verified official websites |
|---|---|---|---|---|
| 100 random companies (cached registry) | 100/100 | 331 | 2.3 min | 11 |
| 1,000 random companies (cached registry, 2,000-request cap) | 1000/1000 | 1,999 | 4.6 min | 96 |
| clean venv, empty cache, 5 companies | 5/5 | 100 | 17.5 min (16 min = 350 MB registry download) | — |

With a 2,000-request cap and 1,000 companies the request budget, not time, is the binding limit; a larger
`--max-requests` lets the name-derived domain pass cover every candidate.

## Limitations

* Most Norwegian entities are small holding/property companies with no web presence; they get full registry and
  accounts coverage and explicit `not_available` web states.
* Websites that are neither listed in the registry nor derivable from the name are not discovered (no search API).
* NAV covers most, not all, Norwegian job ads (no FINN.no ads); the feed window is 40 days by default.
* JavaScript-only sites are fetched as static HTML; their content may be thin.
