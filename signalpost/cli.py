"""One-command batch runner: organisation numbers in, exactly one terminal envelope per input out."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import AGENT_NAME, __version__
from .budget import Budget
from .claims import (SECTIONS, Envelope, add_financial_claims, add_nav_claims, add_registry_activity, add_registry_claims,
                     add_role_claims, add_subunit_claims, add_web_claims, normalize_roles)
from .financials import fetch_accounts
from .http import Http, utc_now
from .refresh import StateStore, apply_refresh, state_from_envelope
from .registry import fetch_entity_live, fetch_roles_live, fetch_subunits_live, load_registry
from .synthesis import build_summary
from .viewer import write_viewer
from .claims import add_directory_claims, add_wikidata_claims, add_workforce_claims
from .directory import fetch_directory_page, sitemap_hits
from .nav_jobs import NavJobs
from .wikidata import fetch_wikidata
from .workforce_runner import WorkforceRunner, default_workers, estimated_capacity
from .registry import host_key
from .website import DIRECTORY_HOSTS, PLATFORM_HOSTS, Candidate, SiteCrawler, build_candidates, guess_candidates


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------------------- input

def read_inputs(path: Path) -> list[str]:
    """Accept JSON (list or {organisation_numbers}), JSONL, CSV or plain text. Order is preserved; raw tokens kept."""
    text = path.read_text(encoding="utf-8-sig")
    stripped = text.strip()
    items: list = []
    whole = _json_or_none(stripped)
    if isinstance(whole, list):
        items = whole
    elif isinstance(whole, dict) and not stripped.count("\n"):
        items = whole.get("organisation_numbers") or whole.get("organisations") or whole.get("companies") or whole.get("items") or [whole]
    elif isinstance(whole, dict):
        items = whole.get("organisation_numbers") or whole.get("organisations") or whole.get("companies") or whole.get("items") or [whole]
    elif stripped.startswith("{") or stripped.startswith('"'):
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif "," in stripped.splitlines()[0] if stripped else False:
        items = list(csv.DictReader(stripped.splitlines()))
        if items and not any(k in items[0] for k in ("organisation_number", "orgnr", "organisasjonsnummer", "organisationNumber", "org_number", "id")):
            items = [line.split(",")[0] for line in stripped.splitlines()]
    else:
        items = [line.strip() for line in text.splitlines() if line.strip()]
    out = []
    for item in items:
        if isinstance(item, dict):
            value = next((item[k] for k in ("organisation_number", "organisationNumber", "orgnr", "org_number", "organisasjonsnummer", "id") if k in item), "")
        else:
            value = item
        out.append(str(value).strip())
    return [v for v in out if v and v.lower() not in {"organisation_number", "orgnr"}]


def _json_or_none(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def normalize_org(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    return digits if len(digits) == 9 else None


# ------------------------------------------------------------------------------- run

def run(args) -> int:
    started_at = utc_now()
    run_id = args.run_id or datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    budget = Budget(args.max_requests, args.time_budget_min * 60, reserve_s=max(120, args.time_budget_min * 60 * 0.18))
    http = Http(budget, workers=args.workers)
    raw_inputs = read_inputs(Path(args.input))
    orgs: list[str] = []
    invalid: list[str] = []
    for raw in raw_inputs:
        org = normalize_org(raw)
        if org is None:
            invalid.append(raw)
        orgs.append(org or raw)
    unique = list(dict.fromkeys(o for o in orgs if normalize_org(o)))
    log(f"{AGENT_NAME} {__version__}: {len(raw_inputs)} inputs ({len(unique)} unique valid), budget {args.max_requests} requests / {args.time_budget_min} min")

    # Mandatory phases first: registry bulk (3) and one accounts call per company.
    budget.reserve(3, "registry")
    registry = load_registry(http, unique, Path(args.cache_dir), max_age_hours=args.registry_max_age_hours,
                             provided_dir=Path(args.registry_dir) if args.registry_dir else None, log=log,
                             entity_file=Path(args.bulk) if args.bulk else None)
    budget.release("registry")
    fin_reserved = (not args.no_financials) and budget.reserve(len(unique), "accounts")

    envs: dict[str, Envelope] = {org: Envelope(org, run_id, started_at) for org in unique}
    entities: dict[str, dict] = {}
    roles: dict[str, list[dict] | None] = {}
    subunits: dict[str, list[dict] | None] = {}
    live_flags: dict[str, set[str]] = {org: set() for org in unique}

    # Registry fallbacks (only when a bulk file failed).
    def registry_fallback(org: str):
        entity = registry.entities.get(org)
        if entity is None:  # bulk failed, or entity registered after the snapshot
            entity, _ = fetch_entity_live(http, org)
            if entity:
                live_flags[org].add("entities")
        rbody = registry.roles.get(org)
        if rbody is None and registry.sources.get("roles", {}).get("error"):
            rbody, _ = fetch_roles_live(http, org)
            if rbody is not None:
                live_flags[org].add("roles")
        subs = registry.subunits.get(org)
        if subs is None and registry.sources.get("subunits", {}).get("error"):
            subs, _ = fetch_subunits_live(http, org)
            if subs is not None:
                live_flags[org].add("subunits")
        elif subs is None:
            subs = []  # bulk parsed fine and holds no workplaces for this organisation
        return org, entity, rbody, subs

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for org, entity, rbody, subs in pool.map(registry_fallback, unique):
            entities[org] = entity
            roles[org] = normalize_roles(rbody) if rbody is not None else ([] if not registry.sources.get("roles", {}).get("error") else None)
            subunits[org] = subs

    # Write provisional envelopes immediately so a hard kill can never drop a row.
    write_outputs(out_dir, raw_inputs, orgs, envs, entities, {}, run_id, started_at, budget, registry, provisional=True)

    # CPU-bound workforce connector runs in the background for the whole run (OCR of official annual-account copies).
    runner = None
    if not args.no_workforce:
        jobs = [(org, entities[org]["latest_submitted_accounts"]) for org in unique
                if entities.get(org) and str(entities[org].get("latest_submitted_accounts") or "").isdigit()]
        jobs.sort(key=lambda j: (bool(entities[j[0]].get("employees")), entities[j[0]].get("legal_form") not in ("AS", "ASA")))
        workers = args.ocr_workers or default_workers()
        capacity = estimated_capacity(workers, budget.soft_deadline - time.monotonic() - 60)
        # Requests not needed by mandatory phases: accounts are reserved separately; ~150 cover NAV, Wikidata and directory.
        free = max(0, budget.remaining() - (0 if fin_reserved else len(unique)) - 150)
        cap = min(len(jobs), capacity, int(free * args.workforce_share))
        if cap and budget.reserve(cap, "workforce"):
            runner = WorkforceRunner(http, jobs, request_cap=cap, workers=workers, log=log, cache_dir=Path(args.cache_dir) / "ocr")
            runner.start()
            log(f"workforce: started for {len(jobs)} filings (reserved {cap} requests; OCR capacity ~{capacity} with {workers} workers)")
        else:
            log("workforce: no request budget or time available")

    # Phase A: official accounts.
    filings: dict[str, tuple] = {}
    if not args.no_financials:
        log(f"accounts: fetching {len(unique)} companies")

        def accounts(org):
            if budget.past_soft_deadline():
                return org, None, None
            return (org, *fetch_accounts(http, org, reserved="accounts" if fin_reserved else False))

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for org, parsed, res in pool.map(accounts, unique):
                filings[org] = (parsed, res)
        budget.release("accounts")
        log(f"accounts: done ({budget.requests} requests used)")

    # Phase A2: official NAV job feed (exact employer organisation number).
    nav_ads: dict[str, list[dict]] = {}
    nav_status = {"loaded": False, "days": args.jobs_days}
    if not args.no_jobs:
        nav = NavJobs(http, days=args.jobs_days, log=log)
        try:
            nav.load()
            nav_status.update(loaded=nav.error is None or nav.error == "stopped_early_budget" and nav.pages > 0, error=nav.error,
                              ads=len(nav.ads), pages=nav.pages, retrieved_at=nav.retrieved_at)
            names = {org: {entities[org]["name"]} | {s.get("name") for s in (subunits.get(org) or []) if s.get("name")} for org in unique if entities.get(org)}
            matches = nav.candidates(names)
            jobs_budget = max(0, min(budget.remaining() // 4, 400))
            work = []
            for org, ads in matches.items():
                for ad in sorted(ads, key=lambda a: a.get("modified") or "", reverse=True)[:12]:
                    work.append((org, ad))
            work = work[:jobs_budget]
            allowed = {org: {org} | {s.get("organisation_number") for s in (subunits.get(org) or [])} for org in matches}

            def confirm(item):
                org, ad = item
                return org, nav.confirm(ad, allowed[org])

            with ThreadPoolExecutor(max_workers=min(8, args.workers)) as pool:
                for org, confirmed in pool.map(confirm, work):
                    if confirmed:
                        nav_ads.setdefault(org, []).append(confirmed)
            log(f"nav: {len(work)} candidate ads checked, {sum(len(v) for v in nav_ads.values())} confirmed for {len(nav_ads)} companies")
        except Exception as exc:
            nav_status.update(loaded=False, error=f"{type(exc).__name__}: {exc}")
            log(f"nav: failed ({exc})")

    # Phase A3: open data and directory (a handful of requests for the whole batch).
    wikidata: dict[str, dict] = {}
    if not args.no_wikidata:
        try:
            wikidata, wmeta = fetch_wikidata(http, unique)
            log(f"wikidata: {len(wikidata)} entities ({wmeta.get('error') or 'ok'})")
        except Exception as exc:
            log(f"wikidata: failed ({exc})")
    directory_pages: dict[str, dict] = {}
    if not args.no_directory:
        try:
            hits, dmeta = sitemap_hits(http, set(unique))
            with ThreadPoolExecutor(max_workers=4) as pool:
                for org, page in zip(hits, pool.map(lambda o: fetch_directory_page(http, o, hits[o]), hits)):
                    directory_pages[org] = page
            log(f"directory: {len(hits)} pages ({dmeta.get('error') or 'ok'})")
        except Exception as exc:
            log(f"directory: failed ({exc})")

    # Phase B: website discovery + crawl, ordered by expected value.
    sites: dict[str, object] = {}
    attempts: dict[str, list] = {org: [] for org in unique}
    cands: dict[str, tuple] = {}
    for org in unique:
        if entities.get(org):
            found, declared = build_candidates(org, entities[org], subunits.get(org) or [], registry)
            known = {c.host for c in found}
            for ad in nav_ads.get(org, []):
                home = ad.get("employer_homepage")
                host = host_key(home)
                if home and host and host not in known and not any(p in host for p in PLATFORM_HOSTS + DIRECTORY_HOSTS) and registry.owners(host) <= {org}:
                    url = home if home.startswith("http") else "https://" + home
                    found.insert(0 if not found or found[0].origin != "registry_website" else 1,
                                 Candidate(url=url, host=host, origin="nav_employer_homepage", exclusive=True, owners=1))
                    known.add(host)
            extra = [(u, "wikidata_website") for u in (wikidata.get(org) or {}).get("sites", [])]
            extra += [(u, "directory_listing") for u in ((directory_pages.get(org) or {}).get("same_as") or []) if isinstance(u, str)]
            for raw_url, origin in extra:
                host = host_key(raw_url)
                if host and host not in known and not any(p in host for p in PLATFORM_HOSTS + DIRECTORY_HOSTS) and registry.owners(host) <= {org}:
                    url = raw_url if raw_url.lower().startswith("http") else "https://" + raw_url
                    found.append(Candidate(url=url, host=host, origin=origin, exclusive=True, owners=1))
                    known.add(host)
            cands[org] = (found, declared)
        else:
            cands[org] = ([], [])
    crawl_queue = [org for org in unique if cands[org][0]]
    crawl_queue.sort(key=lambda o: (cands[o][0][0].origin != "registry_website", not cands[o][0][0].exclusive))
    if args.no_web:
        crawl_queue = []
    log(f"web: {len(crawl_queue)} companies with website candidates; {budget.remaining()} requests left")
    crawler = SiteCrawler(http)
    verified = ("exact", "strong", "registry_declared")

    def crawl(org, candidates, pending_count, max_pages):
        result = sites.get(org)
        for candidate in candidates:
            if budget.past_soft_deadline():
                break
            left = budget.remaining()
            if left < 3:
                break
            page_budget = max(2, min(max_pages, left // max(1, pending_count // 2)))
            if not candidate.exclusive:
                page_budget = 1  # shared manager/group domains: confirm reachability only
            try:
                attempt = crawler.crawl(org, entities[org], roles.get(org) or [], candidate, page_budget)
            except Exception as exc:  # one bad site must never break the batch
                attempts[org].append({"host": candidate.host, "origin": candidate.origin, "identity": "failed", "error": type(exc).__name__})
                continue
            attempts[org].append({"host": candidate.host, "origin": candidate.origin, "identity": attempt.identity, "notes": attempt.notes[-2:],
                                  "pages": len(attempt.pages)})
            # Keep the most informative outcome: verified > shared_site > anything else.
            rank = {"exact": 5, "strong": 4, "registry_declared": 3, "shared_site": 2}
            if result is None or rank.get(attempt.identity, 0) > rank.get(result.identity, 0) or (result.identity in ("unreachable",) and attempt.identity != "unreachable"):
                result = attempt
            if attempt.identity in verified:
                break
        return org, result

    def run_pass(queue, pick, max_pages, label):
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(crawl, org, pick(org), len(queue), max_pages) for org in queue]
            for future in as_completed(futures):
                org, result = future.result()
                if result is not None:
                    sites[org] = result
        log(f"web[{label}]: {len(queue)} companies; verified so far {sum(1 for s in sites.values() if s.identity in verified)}; "
            f"{budget.requests} requests used, {budget.elapsed():.0f}s")

    run_pass(crawl_queue, lambda o: cands[o][0][:3], args.pages_per_site, "registry")
    # Pass 2: name-derived domains for companies still without a verified site (hard-proof gate).
    if not args.no_web and not args.no_guess:
        guess_queue = []
        for org in unique:
            if not entities.get(org) or (sites.get(org) is not None and sites[org].identity in verified + ("shared_site",)):
                continue
            guesses = guess_candidates(org, entities[org], registry)
            if guesses:
                cands[org] = (cands[org][0] + guesses, cands[org][1]) if cands[org][0] else (guesses, cands[org][1])
                guess_queue.append(org)
        guess_queue.sort(key=lambda o: -int(float(entities[o].get("employees") or 0)))
        guess_map = {o: [c for c in cands[o][0] if c.origin == "name_guess"] for o in guess_queue}
        run_pass(guess_queue, lambda o: guess_map[o], 4, "name-guess")

    # Let OCR use the remaining time up to the soft deadline.
    workforce_results: dict[str, dict] = {}
    workforce_status: dict = {"skipped_reason": "Workforce connector disabled for this run."}
    if runner is not None:
        runner.join_until(budget.soft_deadline - time.monotonic())
        workforce_results, workforce_status = dict(runner.results), dict(runner.status)

    # Assemble envelopes.
    store = StateStore(Path(args.state_dir))
    previous_from_file = {}
    if args.previous and Path(args.previous).exists():
        for line in Path(args.previous).read_text(encoding="utf-8").splitlines():
            if line.strip():
                env_prev = json.loads(line)
                previous_from_file[env_prev.get("organisation_number")] = state_from_envelope(env_prev)
    now = utc_now()
    for org in unique:
        env = envs[org]
        try:
            entity = entities.get(org)
            if not entity:
                env.errors.append({"stage": "registry", "error": "organisation number not found in the registry snapshot or live API"})
                env.absent("legal_identity_and_brand", "legal_name", "not_available", "Organisation number not present in the official registry snapshot.")
            else:
                meta = registry.sources
                add_registry_claims(env, entity, meta.get("entities", {}), live="entities" in live_flags[org])
                add_role_claims(env, roles.get(org), meta.get("roles", {}), live="roles" in live_flags[org])
                add_subunit_claims(env, subunits.get(org), meta.get("subunits", {}), live="subunits" in live_flags[org])
                parsed, res = filings.get(org, (None, None))
                if args.no_financials:
                    env.absent("annual_accounts", "annual_accounts", "blocked", "Accounts fetching disabled for this run.")
                elif res is None:
                    env.absent("annual_accounts", "annual_accounts", "blocked", "Time budget reached before the accounts call.")
                else:
                    add_financial_claims(env, parsed, res, entity)
                    env.requests += 1
                site = sites.get(org)
                add_nav_claims(env, nav_ads.get(org), nav_status) if not args.no_jobs else None
                add_web_claims(env, cands[org][0], cands[org][1], site, attempts.get(org))
                add_wikidata_claims(env, wikidata.get(org))
                add_directory_claims(env, directory_pages.get(org))
                add_workforce_claims(env, workforce_results.get(org), workforce_status, bool(entity.get("employees")))
                add_registry_activity(env, entity, roles.get(org), parsed)
                env.drop_superseded_absences()
            prev = previous_from_file.get(org) or store.load(org)
            web_checked = org in sites and sites[org] is not None and sites[org].identity not in ("blocked",)
            changes, state = apply_refresh(env, prev, now, web_checked or not cands[org][0])
            env.changes = changes
            env.previous_run_id = (prev or {}).get("last_run_id")
            env.summary = build_summary(env, changes)
            if not args.dry_state:
                store.save(org, state)
        except Exception as exc:
            env.errors.append({"stage": "assemble", "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=3)})
            env.failed = True
    report = write_outputs(out_dir, raw_inputs, orgs, envs, entities, sites, run_id, started_at, budget, registry, provisional=False)
    report["connectors"] = {
        "workforce_annual_accounts_ocr": workforce_status,
        "nav_job_feed": {k: v for k, v in nav_status.items() if k != "retrieved_at"},
        "nav_confirmed_companies": len(nav_ads),
        "wikidata_entities": len(wikidata),
        "directory_pages": len(directory_pages),
    }
    (out_dir / "run-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_viewer(out_dir / "envelopes.jsonl", out_dir / "report.html")
    log(f"done: {report['emitted_envelopes']} envelopes, {report['operations']['requests']} requests, {report['operations']['elapsed_s']}s -> {out_dir}")
    return 0 if report["validation"]["passed"] else 1


# ------------------------------------------------------------------------------- output

def envelope_dict(env: Envelope, entity: dict | None, site, completed_at: str, provisional: bool) -> dict:
    failed = getattr(env, "failed", False)
    status = "failed" if failed else ("provisional" if provisional else "completed")
    sections = env.section_states()
    sections["claim_evidence"] = {"label": dict(SECTIONS)["claim_evidence"], "state": "available" if env.claims else "failed",
                                  "claims": len(env.claims), "evidence_records": len(env.evidence)}
    changes = getattr(env, "changes", [])
    sections["refresh"] = {"label": dict(SECTIONS)["refresh"], "state": "available", "previous_run_id": getattr(env, "previous_run_id", None),
                           "changes": len(changes), "material_changes": sum(1 for c in changes if c.get("material"))}
    overall = "failed" if failed else ("available" if any(c["availability"] == "available" for c in env.claims) else "not_available")
    return {
        "organisation_number": env.org,
        "legal_name": (entity or {}).get("name"),
        "state": overall,
        "run": {"run_id": env.run_id, "agent": AGENT_NAME, "agent_version": __version__, "started_at": env.started_at,
                "completed_at": completed_at, "terminal_status": status},
        "summary": getattr(env, "summary", None),
        "sections": sections,
        "claims": env.claims,
        "evidence": list(env.evidence.values()),
        "changes": changes,
        "refresh": {"previous_run_id": getattr(env, "previous_run_id", None), "idempotent_keys": "section|key",
                    "history_store": "state/companies/<org>.json"},
        "website_identity": None if site is None else {"candidate": site.candidate.host, "origin": site.candidate.origin,
                                                       "identity": site.identity, "proofs": site.proofs, "pages": site.pages, "notes": site.notes},
        "errors": env.errors,
        "operations": {"third_party_cost_usd": 0, "website_requests": len(site.pages) if site else 0},
    }


def write_outputs(out_dir: Path, raw_inputs, orgs, envs, entities, sites, run_id, started_at, budget, registry, provisional: bool) -> dict:
    completed_at = utc_now()
    lines, emitted, seen = [], 0, set()
    for raw, org in zip(raw_inputs, orgs):
        if org in envs and org not in seen:
            seen.add(org)
            record = envelope_dict(envs[org], entities.get(org), sites.get(org) if sites else None, completed_at, provisional)
        else:
            reason = "duplicate input organisation number" if org in seen else "input is not a valid 9-digit Norwegian organisation number"
            record = {"organisation_number": org if org in envs else raw, "input": raw, "state": "failed",
                      "run": {"run_id": run_id, "agent": AGENT_NAME, "agent_version": __version__, "started_at": started_at,
                              "completed_at": completed_at, "terminal_status": "failed"},
                      "summary": None, "sections": {}, "claims": [], "evidence": [], "changes": [],
                      "errors": [{"stage": "input", "error": reason}], "operations": {"third_party_cost_usd": 0}}
            if org in envs:
                record["duplicate_of_input"] = True
        lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        emitted += 1
    target = out_dir / "envelopes.jsonl"
    tmp = target.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(target)
    states = {}
    for env in envs.values():
        for c in env.claims:
            states[c["availability"]] = states.get(c["availability"], 0) + 1
    web = {}
    for site in (sites or {}).values():
        if site is not None:
            web[site.identity] = web.get(site.identity, 0) + 1
    report = {
        "run_id": run_id, "agent": AGENT_NAME, "agent_version": __version__, "provisional": provisional,
        "started_at": started_at, "completed_at": completed_at, "inputs": len(raw_inputs), "emitted_envelopes": emitted,
        "operations": budget.snapshot(), "registry_sources": registry.sources, "registry_errors": registry.errors,
        "claim_states": states, "website_identity_outcomes": web,
        "companies_with_verified_website": sum(1 for s in (sites or {}).values() if s is not None and s.identity in ("exact", "strong", "registry_declared")),
        "validation": {"passed": emitted == len(raw_inputs), "exact_one_envelope_per_input": emitted == len(raw_inputs),
                       "unique_valid_organisations": len(envs)},
        "models_and_apis": {"llm": None, "third_party_paid_apis": None, "cost_usd": 0.0},
    }
    (out_dir / "run-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="signalpost", description="Research Norwegian companies from organisation numbers with claim-level evidence.")
    parser.add_argument("--input", "-i", required=True, help="Batch file: JSONL/JSON/CSV/TXT of organisation numbers")
    parser.add_argument("--output-dir", "-o", default="out", help="Writes envelopes.jsonl, run-report.json and report.html")
    parser.add_argument("--run-id")
    parser.add_argument("--max-requests", type=int, default=int(os.environ.get("SIGNALPOST_MAX_REQUESTS", 2000)))
    parser.add_argument("--time-budget-min", type=float, default=float(os.environ.get("SIGNALPOST_TIME_BUDGET_MIN", 45)))
    parser.add_argument("--workers", type=int, default=int(os.environ.get("SIGNALPOST_WORKERS", 16)))
    parser.add_argument("--pages-per-site", type=int, default=7)
    parser.add_argument("--state-dir", default=os.environ.get("SIGNALPOST_STATE_DIR", "state"), help="Refresh history store")
    parser.add_argument("--previous", help="Optional previous envelopes.jsonl to diff against")
    parser.add_argument("--cache-dir", default=os.environ.get("SIGNALPOST_CACHE_DIR", ".cache"))
    parser.add_argument("--registry-dir", default=os.environ.get("SIGNALPOST_REGISTRY_DIR"), help="Evaluator-supplied Brreg bulk files")
    parser.add_argument("--bulk", default=os.environ.get("SIGNALPOST_BULK"), help="Brreg entity snapshot file (starter-kit compatible)")
    parser.add_argument("--registry-max-age-hours", type=float, default=20)
    parser.add_argument("--no-web", action="store_true")
    parser.add_argument("--no-financials", action="store_true")
    parser.add_argument("--no-guess", action="store_true", help="Disable name-derived domain discovery")
    parser.add_argument("--no-jobs", action="store_true", help="Disable the NAV job feed connector")
    parser.add_argument("--jobs-days", type=int, default=int(os.environ.get("SIGNALPOST_JOBS_DAYS", 40)))
    parser.add_argument("--no-workforce", action="store_true", help="Disable OCR of annual-account copies")
    parser.add_argument("--workforce-share", type=float, default=float(os.environ.get("SIGNALPOST_WORKFORCE_SHARE", 0.7)),
                        help="Share of the non-mandatory request budget available for annual-account copies")
    parser.add_argument("--ocr-workers", type=int, default=int(os.environ["SIGNALPOST_OCR_WORKERS"]) if os.environ.get("SIGNALPOST_OCR_WORKERS") else None)
    parser.add_argument("--no-wikidata", action="store_true")
    parser.add_argument("--no-directory", action="store_true")
    parser.add_argument("--dry-state", action="store_true", help="Do not update the refresh store")
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        return run(args)
    finally:
        log(f"total wall time {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    raise SystemExit(main())
