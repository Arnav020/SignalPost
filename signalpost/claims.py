"""Claim/evidence model and the per-company terminal envelope."""
from __future__ import annotations

import hashlib
import json

from .financials import ACCOUNT_COPY, ACCOUNTS_API
from .registry import ENTITY_API, ENTITY_PAGE, ROLES_API, SUBUNITS_API

SECTIONS = [
    ("legal_identity_and_brand", "Legal identity and public brand"),
    ("annual_accounts", "Latest annual accounts and available history"),
    ("leadership_and_workplaces", "Leadership and registered workplaces"),
    ("website_and_profiles", "Verified official website and company-owned profiles"),
    ("hiring_and_activity", "Hiring and dated public activity"),
    ("claim_evidence", "Claim-level evidence and availability state"),
    ("refresh", "Refresh metadata and material changes"),
]
STATES = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}
CONFIDENCE = {"exact": 0.98, "strong": 0.9, "registry_declared": 0.75}
# External field families, named after Builderr's own observation signal types.
SIGNAL_TYPES = {
    "official_website": "company_profile", "public_brand_name": "company_profile", "website_description": "company_profile",
    "wikidata_entity": "company_profile", "directory_profile": "company_profile", "employer_self_description": "company_profile",
    "social_profile": "profile_handle", "site_snapshot": "profile_metrics", "website_contact_email": "company_profile",
    "website_contact_phone": "company_profile", "website_address": "place_summary", "news_item": "public_post",
    "wikipedia_article": "public_mention", "job_posting": "job_posting",
    "hiring_status": "job_posting", "careers_page": "company_profile", "recruiting_system_link": "company_profile",
    "workforce": "workforce_snapshot", "wikidata_employees": "workforce_snapshot", "review_summary": "review_summary",
    "leader_named_on_website": "company_profile", "shared_website": "company_profile",
}


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def truthy(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "ja", "yes"}


def normalize_roles(body: dict | None) -> list[dict]:
    roles = []
    for group in (body or {}).get("rollegrupper", []) or []:
        changed = group.get("sistEndret")
        gtype = group.get("type") or {}
        for item in group.get("roller", []) or []:
            person = item.get("person") or {}
            name = person.get("navn") or {}
            entity = item.get("enhet") or {}
            display = " ".join(filter(None, [name.get("fornavn"), name.get("mellomnavn"), name.get("etternavn")]))
            if not display and entity:
                display = " ".join(entity.get("navn") or []) if isinstance(entity.get("navn"), list) else entity.get("navn")
            rtype = item.get("type") or {}
            roles.append({
                "name": display or None,
                "person": bool(person),
                "deceased": bool(person.get("erDoed")),
                "organisation_number": entity.get("organisasjonsnummer"),
                "role_code": rtype.get("kode"),
                "role": rtype.get("beskrivelse"),
                "group_code": gtype.get("kode"),
                "group": gtype.get("beskrivelse"),
                "group_last_changed": changed,
                "inactive": bool(item.get("avregistrert") or item.get("fratraadt")),
                "order": item.get("rekkefolge"),
            })
    return roles


class Envelope:
    def __init__(self, org: str, run_id: str, started_at: str):
        self.org = org
        self.run_id = run_id
        self.started_at = started_at
        self.claims: list[dict] = []
        self.evidence: dict[str, dict] = {}
        self.errors: list[dict] = []
        self.section_notes: dict[str, list[str]] = {key: [] for key, _ in SECTIONS}
        self.requests = 0

    # evidence -------------------------------------------------------------------
    def add_evidence(self, *, source_url: str, source_class: str, retrieved_at: str, content_sha256: str | None,
                     span: str | None = None, method: str, reporting_period: dict | None = None, extra: dict | None = None) -> str:
        record = {
            "source_url": source_url,
            "source_class": source_class,
            "retrieved_at": retrieved_at,
            "content_sha256": content_sha256,
            "claim_span": span,
            "extraction_method": method,
        }
        if reporting_period:
            record["reporting_period"] = reporting_period
        if extra:
            record.update(extra)
        key = "ev-" + _sha(record)[:12]
        record["id"] = key
        self.evidence.setdefault(key, record)
        return key

    def claim(self, section: str, field: str, value, evidence_ids: list[str], *, key: str | None = None,
              availability: str = "available", confidence: float = 1.0, **extra) -> dict:
        assert availability in STATES
        item = {
            "key": key or field,
            "section": section,
            "field": field,
            "value": value,
            "availability": availability,
            "confidence": round(confidence, 3),
            "evidence_ids": evidence_ids,
        }
        item.update({k: v for k, v in extra.items() if v is not None})
        if field in SIGNAL_TYPES:
            item["signal_type"] = SIGNAL_TYPES[field]
            item["external"] = True
        self.claims.append(item)
        return item

    def absent(self, section: str, field: str, availability: str, reason: str, evidence_ids: list[str] | None = None, **extra) -> dict:
        return self.claim(section, field, None, evidence_ids or [], availability=availability, confidence=1.0, reason=reason, **extra)

    def drop_superseded_absences(self) -> None:
        """An absence record is dropped when another source produced the same field; duplicate keys keep the first."""
        found = {(c["section"], c["field"]) for c in self.claims if c["availability"] == "available"}
        seen, kept = set(), []
        for c in self.claims:
            if c["availability"] != "available" and (c["section"], c["field"]) in found:
                continue
            ident = (c["section"], c["key"], c["availability"])
            if ident in seen:
                continue
            seen.add(ident)
            kept.append(c)
        self.claims = kept

    # output ---------------------------------------------------------------------
    def section_states(self) -> dict:
        states = {}
        for key, label in SECTIONS:
            if key in ("claim_evidence", "refresh"):
                continue
            items = [c for c in self.claims if c["section"] == key]
            available = [c for c in items if c["availability"] == "available"]
            if available:
                state = "available"
            elif any(c["availability"] == "blocked" for c in items):
                state = "blocked"
            elif any(c["availability"] == "ambiguous" for c in items):
                state = "ambiguous"
            elif any(c["availability"] == "failed" for c in items):
                state = "failed"
            elif any(c["availability"] == "not_applicable" for c in items) and not any(c["availability"] == "not_available" for c in items):
                state = "not_applicable"
            else:
                state = "not_available"
            states[key] = {"label": label, "state": state, "available_claims": len(available), "claims": len(items),
                           "notes": self.section_notes.get(key) or None}
        return states


# ---------------------------------------------------------------------- registry claims

def add_registry_claims(env: Envelope, entity: dict, registry_meta: dict, live: bool = False) -> None:
    org = env.org
    sec = "legal_identity_and_brand"
    snap = registry_meta or {}
    ev = env.add_evidence(
        source_url=ENTITY_API.format(org=org), source_class="official_registry", retrieved_at=snap.get("retrieved_at") or env.started_at,
        content_sha256=_sha(entity), span=f"organisasjonsnummer={org}; navn={entity.get('name')}",
        method="brreg_entity_live_api" if live else "brreg_bulk_snapshot_row",
        extra={"public_page": ENTITY_PAGE.format(org=org), "snapshot_url": None if live else snap.get("url"),
               "snapshot_sha256": None if live else snap.get("sha256")},
    )
    E = [ev]
    env.claim(sec, "organisation_number", org, E)
    env.claim(sec, "legal_name", entity.get("name"), E)
    if entity.get("legal_form"):
        env.claim(sec, "legal_form", {"code": entity["legal_form"], "label": entity.get("legal_form_label")}, E)
    status = "active"
    if truthy(entity.get("bankrupt")):
        status = "bankrupt"
    elif truthy(entity.get("forced_dissolution")):
        status = "forced_dissolution"
    elif truthy(entity.get("liquidating")):
        status = "under_liquidation"
    env.claim(sec, "registry_status", status, E, effective_date=entity.get("bankrupt_at") or entity.get("liquidating_at") or None)
    if entity.get("industry_code"):
        env.claim(sec, "industry", {"nace": entity["industry_code"], "label": entity.get("industry_label")}, E, key="industry.primary")
    if entity.get("industry2_code"):
        env.claim(sec, "industry", {"nace": entity["industry2_code"], "label": entity.get("industry2_label")}, E, key="industry.secondary")
    if entity.get("sector"):
        env.claim(sec, "institutional_sector", entity["sector"], E)
    street = (entity.get("business_street") or "").replace("\n", ", ")
    if street or entity.get("business_postcode"):
        env.claim(sec, "business_address", {"street": street or None, "postcode": entity.get("business_postcode") or None,
                                            "city": entity.get("business_city") or None, "municipality": entity.get("municipality") or None,
                                            "country": entity.get("country") or None}, E)
    if entity.get("postal_street") or entity.get("postal_postcode"):
        env.claim(sec, "postal_address", {"street": (entity.get("postal_street") or "").replace("\n", ", ") or None,
                                          "postcode": entity.get("postal_postcode") or None, "city": entity.get("postal_city") or None}, E)
    if entity.get("founded_at"):
        env.claim(sec, "founded_date", entity["founded_at"], E, effective_date=entity["founded_at"])
    if entity.get("registered_at"):
        env.claim(sec, "registered_date", entity["registered_at"], E, effective_date=entity["registered_at"])
    if entity.get("in_business_register"):
        env.claim(sec, "in_business_register", truthy(entity["in_business_register"]), E, effective_date=entity.get("business_registered_at") or None)
    if entity.get("vat_registered"):
        env.claim(sec, "vat_registered", truthy(entity["vat_registered"]), E)
    if entity.get("employees"):
        env.claim(sec, "registered_employees", int(float(entity["employees"])), E, effective_date=entity.get("employees_registered_at") or None,
                  note="Employee count as registered in Enhetsregisteret (from the NAV Aa-register).")
        env.claim("hiring_and_activity", "workforce", {"value": int(float(entity["employees"])), "measure": "registered_employees",
                                                       "label": "employees registered in Enhetsregisteret (NAV Aa-register)"},
                  E, key="workforce.registry", effective_date=entity.get("employees_registered_at") or None)
    elif truthy(entity.get("has_registered_employees")):
        env.absent(sec, "registered_employees", "not_available", "The registry flags registered employees but publishes no count.", E)
    else:
        env.absent(sec, "registered_employees", "not_available", "No employee count is registered for this entity.", E)
    if entity.get("phone") or entity.get("mobile"):
        env.claim(sec, "registry_phone", entity.get("phone") or entity.get("mobile"), E)
    if entity.get("email"):
        env.claim(sec, "registry_email", entity["email"].strip().lower(), E)
    if entity.get("share_capital"):
        try:
            amount = float(entity["share_capital"])
            env.claim(sec, "share_capital", {"amount": amount, "currency": entity.get("share_capital_currency") or "NOK",
                                             "type": entity.get("share_capital_type") or None}, E,
                      effective_date=entity.get("share_capital_registered_at") or None)
        except ValueError:
            pass
    purpose = (entity.get("statutory_purpose") or "").strip()
    activity = (entity.get("activity") or "").strip()
    if purpose:
        env.claim(sec, "statutory_purpose", purpose, E, effective_date=entity.get("articles_dated") or None)
    if activity:
        env.claim(sec, "registered_activity", activity, E)


def add_role_claims(env: Envelope, roles: list[dict], meta: dict, live: bool = False) -> None:
    sec = "leadership_and_workplaces"
    if roles is None:
        env.absent(sec, "roles", "failed", "Official roles could not be retrieved in this run.")
        return
    ev = env.add_evidence(source_url=ROLES_API.format(org=env.org), source_class="official_registry",
                          retrieved_at=(meta or {}).get("retrieved_at") or env.started_at, content_sha256=_sha(roles),
                          span=None, method="brreg_roles_live_api" if live else "brreg_roles_bulk_snapshot",
                          extra={"snapshot_url": None if live else (meta or {}).get("url"), "snapshot_sha256": None if live else (meta or {}).get("sha256"),
                                 "privacy": "dates of birth are discarded"})
    active = [r for r in roles if not r["inactive"] and not r["deceased"]]
    if not active:
        env.absent(sec, "roles", "not_available", "No active roles are registered.", [ev])
        return
    for r in active:
        field = {"DAGL": "ceo", "LEDE": "board_chair", "NEST": "board_deputy_chair", "MEDL": "board_member",
                 "VARA": "board_deputy_member", "REVI": "auditor", "REGN": "accountant", "KONT": "contact_person",
                 "INNH": "owner", "DTPR": "general_partner", "DTSO": "general_partner", "KOMP": "general_partner",
                 "FFØR": "business_manager", "SAM": "joint_owner"}.get(r["role_code"], "role")
        value = {"name": r["name"], "role": r["role"], "role_code": r["role_code"], "is_person": r["person"]}
        if r.get("organisation_number"):
            value["organisation_number"] = r["organisation_number"]
        env.claim(sec, field, value, [ev], key=f"role.{r['role_code']}.{(r['name'] or '').lower()}",
                  effective_date=r.get("group_last_changed"), date_kind="role_group_last_changed")


def add_subunit_claims(env: Envelope, subunits: list[dict] | None, meta: dict, live: bool = False) -> None:
    sec = "leadership_and_workplaces"
    if subunits is None:
        env.absent(sec, "registered_workplace", "failed", "Registered workplaces could not be retrieved in this run.")
        return
    ev = env.add_evidence(source_url=SUBUNITS_API.format(org=env.org), source_class="official_registry",
                          retrieved_at=(meta or {}).get("retrieved_at") or env.started_at, content_sha256=_sha(subunits), span=None,
                          method="brreg_subunits_live_api" if live else "brreg_subunits_bulk_snapshot",
                          extra={"snapshot_url": None if live else (meta or {}).get("url"), "snapshot_sha256": None if live else (meta or {}).get("sha256")})
    open_units = [s for s in subunits if not s.get("closed_at")]
    if not open_units:
        env.absent(sec, "registered_workplace", "not_available", "No open registered workplaces (underenheter).", [ev])
        return
    for s in open_units[:60]:
        value = {"organisation_number": s.get("organisation_number"), "name": s.get("name"),
                 "address": ", ".join(filter(None, [(s.get("street") or "").replace("\n", ", "), " ".join(filter(None, [s.get("postcode"), s.get("city")]))])) or None,
                 "municipality": s.get("municipality") or None, "industry": s.get("industry_label") or None}
        if s.get("employees"):
            value["registered_employees"] = int(float(s["employees"]))
        env.claim(sec, "registered_workplace", value, [ev], key=f"workplace.{s.get('organisation_number')}",
                  effective_date=s.get("started_at") or s.get("registered_at") or None)
    if len(open_units) > 60:
        env.section_notes[sec].append(f"{len(open_units)} open workplaces; first 60 listed.")


def add_financial_claims(env: Envelope, filings, res, entity: dict) -> None:
    sec = "annual_accounts"
    org = env.org
    if filings is None:
        if res is not None and res.status == 404:
            env.absent(sec, "annual_accounts", "not_available", "Regnskapsregisteret returned no structured accounts (404).",
                       [env.add_evidence(source_url=ACCOUNTS_API.format(org=org), source_class="official_annual_accounts",
                                         retrieved_at=res.retrieved_at, content_sha256=None, method="http_status", span="HTTP 404")])
        elif res is not None and res.blocked:
            env.absent(sec, "annual_accounts", "blocked", "Request budget exhausted before the accounts call.")
        else:
            env.absent(sec, "annual_accounts", "failed", f"Accounts request failed ({getattr(res, 'error', None) or getattr(res, 'status', None)}).")
        if entity.get("latest_submitted_accounts"):
            ev = env.add_evidence(source_url=ENTITY_API.format(org=org), source_class="official_registry", retrieved_at=env.started_at,
                                  content_sha256=None, span=f"sisteInnsendteAarsregnskap={entity['latest_submitted_accounts']}", method="brreg_bulk_snapshot_row")
            env.claim(sec, "latest_filed_accounts_year", entity["latest_submitted_accounts"], [ev])
        return
    if not filings:
        env.absent(sec, "annual_accounts", "not_available", "Regnskapsregisteret returned an empty list.")
        return
    latest_ev = None
    ev_by_filing = {}
    for filing in filings:
        period = {"start": filing["period_start"], "end": filing["period_end"]}
        ev = env.add_evidence(source_url=ACCOUNTS_API.format(org=org), source_class="official_annual_accounts",
                              retrieved_at=res.retrieved_at, content_sha256=res.sha256, reporting_period=period,
                              span=f"regnskapsperiode {period['start']}..{period['end']}; regnskapstype={filing['account_type']}; journalnr={filing['journal_number']}",
                              method="regnskapsregisteret_json",
                              extra={"filing_copy_url": ACCOUNT_COPY.format(org=org, year=filing["year"])})
        latest_ev = latest_ev or ev
        ev_by_filing[id(filing)] = ev
        suffix = "" if filing["account_type"] in (None, "SELSKAP") else f".{filing['account_type'].lower()}"
        for field, item in filing["values"].items():
            env.claim(sec, field, item["value"], [ev], key=f"accounts.{filing['year']}{suffix}.{field}", unit=filing["currency"] or "NOK",
                      reporting_period=period, label=item["label"], source_path=item["path"], account_type=filing["account_type"])
        env.claim(sec, "annual_accounts_filing", {"year": filing["year"], "account_type": filing["account_type"],
                                                   "is_parent_company": filing["is_parent_company"], "audit_waived": filing["audit_waived"],
                                                   "small_enterprise_rules": filing["small_enterprise_rules"],
                                                   "liquidation_accounts": filing["liquidation_accounts"],
                                                   "filing_copy_url": ACCOUNT_COPY.format(org=org, year=filing["year"])},
                  [ev], key=f"accounts.{filing['year']}{suffix}.filing", reporting_period=period)
    # Newest filing = the company's own accounts with the latest period end (group accounts are reported separately).
    own = [f for f in filings if (f["account_type"] or "SELSKAP") == "SELSKAP"] or filings
    newest = max(own, key=lambda f: f["period_end"])
    period = {"start": newest["period_start"], "end": newest["period_end"]}
    latest = newest["period_end"][:4]
    registry_latest = str(entity.get("latest_submitted_accounts") or "")
    note = None
    if registry_latest.isdigit() and registry_latest > latest:
        note = (f"The registry records accounts submitted for {registry_latest}; Regnskapsregisteret's structured data currently "
                f"reaches {fiscal_label(period)}.")
        latest = registry_latest
    env.claim(sec, "latest_filed_accounts_year", latest, [ev_by_filing.get(id(newest), latest_ev)], reporting_period=period, fiscal_year_label=fiscal_label(period),
              registry_latest_submitted=registry_latest or None, note=note)


def fiscal_label(period: dict) -> str:
    start, end = period.get("start") or "", period.get("end") or ""
    if start[5:] == "01-01" and end[5:] == "12-31" and start[:4] == end[:4]:
        return f"FY{end[:4]}"
    return f"fiscal period {start} to {end}"


# ---------------------------------------------------------------------- web claims

def _page_evidence(env: Envelope, site, url: str, span: str | None, method: str) -> str:
    record = next((p for p in site.pages if p.get("requested_url") == url or p.get("final_url") == url), None) or (site.pages[0] if site.pages else {})
    return env.add_evidence(source_url=record.get("final_url") or url, source_class="company_owned_website",
                            retrieved_at=record.get("retrieved_at") or env.started_at, content_sha256=record.get("sha256"),
                            span=span, method=method, extra={"identity_level": site.identity})


WEB_FAIL_REASONS = {
    "unreachable": "The candidate website did not respond in this run (timeout or connection error).",
    "parked": "The candidate domain is parked or has no content.",
    "dead_domain": "The candidate domain no longer resolves (no website exists at it).",
    "conflict": "The candidate site shows a different organisation number or redirects to a platform.",
    "ambiguous": "The candidate site could not be tied to this exact organisation.",
    "blocked": "The candidate site blocked automated access (robots.txt or HTTP status) or the budget ran out.",
}


def add_web_claims(env: Envelope, candidates, declared_social: list[dict], site, attempts: list) -> None:
    web = "website_and_profiles"
    act = "hiring_and_activity"
    reg_ev = None
    for social in declared_social:
        reg_ev = reg_ev or env.add_evidence(source_url=ENTITY_API.format(org=env.org), source_class="official_registry", retrieved_at=env.started_at,
                                            content_sha256=None, span=f"hjemmeside={social['raw']}", method="registry_declared_profile")
        env.claim(web, "social_profile", {"platform": social["platform"], "url": social["url"]}, [reg_ev],
                  key=f"social.{social['platform']}.{social['url'].lower()}", confidence=0.95, identity_basis="declared_in_official_registry")

    if site is not None and site.identity == "shared_site" and site.home_url:
        ev = _page_evidence(env, site, site.home_url, site.extracted.get("title") if site.extracted else None, "static_html_fetch")
        env.claim(web, "shared_website", site.home_url, [ev], confidence=0.9, relation="shared_with_other_organisations",
                  discovered_via=site.candidate.origin, note=f"Listed for this organisation but shared by {site.candidate.owners}+ registry organisations "
                  "(e.g. a housing-cooperative manager, accountant or group site); its content is not attributed to this company.")
        env.absent(web, "official_website", "not_available", "Only a shared management/group website is listed; no company-owned website was verified.")
        if not declared_social:
            env.absent(web, "social_profile", "not_available", "No company-owned profile could be verified.")
        env.absent(act, "job_posting", "not_available", "No company-owned jobs source was found.")
        env.absent(act, "news_item", "not_available", "No company-owned news source was found.")
        return

    if site is None or site.identity not in CONFIDENCE:
        if not candidates:
            env.absent(web, "official_website", "not_available",
                       "No website or company email domain is registered for this entity; no permitted discovery source returned a candidate.")
        else:
            if site is None:
                state, reason = "blocked", "Website candidates were not crawled because the run budget was exhausted."
            elif site.candidate.origin == "name_guess":
                state, reason = "not_available", "No website is registered; domains derived from the legal name did not prove to belong to this organisation."
            else:
                state = {"ambiguous": "ambiguous", "conflict": "ambiguous", "blocked": "blocked", "unreachable": "failed"}.get(site.identity, "not_available")
                reason = WEB_FAIL_REASONS.get(site.identity, "The website could not be verified.")
            env.absent(web, "official_website", state, reason,
                       candidates=[{"host": c.host, "origin": c.origin, "exclusive": c.exclusive} for c in candidates][:4], attempts=attempts or None)
        if not declared_social:
            env.absent(web, "social_profile", "not_available", "No company-owned profile could be verified.")
        env.absent(act, "job_posting", "not_available", "No verified company website or permitted jobs source returned postings.")
        env.absent(act, "news_item", "not_available", "No verified company-owned news source was found.")
        return

    conf = CONFIDENCE[site.identity]
    x = site.extracted
    home = x["home_url"]
    proof_span = next((p.get("span") for p in site.proofs if p["type"] == "organisation_number_on_site" and p.get("span")), None)
    proof_url = next((p.get("url") for p in site.proofs if p["type"] == "organisation_number_on_site"), home)
    ev_home = _page_evidence(env, site, proof_url if proof_span else home, proof_span or x.get("title"), "static_html_fetch")
    origin_label = {"registry_website": "declared as hjemmeside in Enhetsregisteret", "registry_email": "domain of the registered email address",
                    "subunit_website": "declared as website of a registered workplace", "subunit_email": "domain of a registered workplace email",
                    "name_guess": "domain derived from the legal name, confirmed on the site",
                    "nav_employer_homepage": "employer homepage in an official NAV job ad carrying this organisation number",
                    "wikidata_website": "official website (P856) on the Wikidata item carrying this organisation number",
                    "directory_listing": "website listed on a directory page carrying this organisation number"}[site.candidate.origin]
    url_only = site.identity == "registry_declared" or getattr(site, "scope", "full") != "full"
    relation = None
    if getattr(site, "scope", "full") != "full":
        relation = "parent_or_group_site" if any(r in ("several_other_organisation_numbers_on_site", "foreign_domain_without_this_organisation_number",
                                                          "group_branded_site", "site_lists_group_companies") for r in site.scope_reasons) else "site_not_named_for_company"
    env.claim(web, "official_website", home, [ev_home], confidence=conf if not url_only else min(conf, 0.75), identity_level=site.identity,
              discovered_via=origin_label, identity_proofs=site.proofs or None, site_relation=relation,
              scope_reasons=getattr(site, "scope_reasons", None) or None,
              note="Only the URL is published: the site appears to belong to a parent group or does not name this company, so its "
                   "profiles, news and jobs are not attributed to this company." if url_only else None)
    if url_only:
        why = ("The website appears to be a parent/group site" if relation == "parent_or_group_site"
               else "The website does not clearly name this company") + "; its content is not attributed to this company."
        if not declared_social:
            env.absent(web, "social_profile", "ambiguous", why)
        env.absent(act, "job_posting", "ambiguous", why)
        env.absent(act, "news_item", "ambiguous", why)
        return
    env.claim(web, "site_snapshot", {"home_url": home, "pages_captured": x.get("pages_captured"),
                                     "verified_social_links": len(x.get("social_profiles") or []),
                                     "structured_organisation_records": len(x.get("jsonld_organizations") or []),
                                     "interpretation": "Observed site surface of the verified company website; not traffic or popularity."},
              [ev_home], confidence=conf)
    if x.get("brand_name"):
        env.claim("legal_identity_and_brand", "public_brand_name", x["brand_name"], [ev_home], confidence=conf)
    if x.get("description"):
        env.claim("legal_identity_and_brand", "website_description", x["description"][:600], [ev_home], confidence=conf, note="Company-reported description.")
    for item in x.get("social_profiles", []):
        ev = _page_evidence(env, site, item["source_url"], item["url"], "html_anchor_or_jsonld_sameAs")
        env.claim(web, "social_profile", {"platform": item["platform"], "url": item["url"]}, [ev], key=f"social.{item['platform']}.{item['url'].lower()}",
                  confidence=conf, identity_basis="linked_from_verified_company_website")
    if not x.get("social_profiles") and not declared_social:
        env.absent(web, "social_profile", "not_available", "The verified website links to no social profiles.", [ev_home])
    for item in x.get("emails", []):
        env.claim(web, "website_contact_email", item["value"], [_page_evidence(env, site, item["source_url"], item["value"], "mailto_link")],
                  key=f"web_email.{item['value']}", confidence=conf)
    for item in x.get("phones", [])[:3]:
        env.claim(web, "website_contact_phone", item["value"], [_page_evidence(env, site, item["source_url"], item["value"], "tel_link")],
                  key=f"web_phone.{item['value']}", confidence=conf)
    for org_ld in x.get("jsonld_organizations", [])[:2]:
        span = json.dumps({k: v for k, v in org_ld.items() if v and k != "source_url"}, ensure_ascii=False)[:300]
        ev = _page_evidence(env, site, org_ld["source_url"], span, "jsonld_organization")
        if org_ld.get("address"):
            env.claim(web, "website_address", org_ld["address"], [ev], key=f"web_address.{org_ld['address'].lower()}", confidence=conf)
        if org_ld.get("founding_date"):
            env.claim(web, "website_founding_date", org_ld["founding_date"], [ev], confidence=conf)
        if org_ld.get("employees"):
            env.claim(web, "website_employee_statement", org_ld["employees"], [ev], confidence=conf, note="Company-reported; not registry data.")
    for leader in x.get("leaders_on_site", []):
        env.claim("leadership_and_workplaces", "leader_named_on_website", leader, [ev_home], key=f"web_leader.{leader['name'].lower()}", confidence=conf)

    if x.get("careers_page"):
        env.claim(act, "careers_page", x["careers_page"], [_page_evidence(env, site, x["careers_page"], None, "careers_link_on_home")], confidence=conf)
    for link in x.get("ats_links", []):
        env.claim(act, "recruiting_system_link", link["url"], [_page_evidence(env, site, link["source_url"], link.get("label") or link["url"], "ats_link")],
                  key=f"ats.{link['url'].lower()}", confidence=conf)
    if x.get("job_postings"):
        for job in x["job_postings"]:
            ev = _page_evidence(env, site, job["source_url"], job.get("title"), job["method"])
            env.claim(act, "job_posting", {"title": job.get("title"), "url": job.get("url"), "location": job.get("location"),
                                           "employment_type": job.get("employment_type")},
                      [ev], key=f"job.{(job.get('url') or job.get('title') or '').lower()}", confidence=conf,
                      effective_date=(job.get("date_posted") or "")[:10] or None,
                      date_kind=job.get("date_kind") or ("date_posted" if job.get("date_posted") else None),
                      valid_through=(job.get("valid_through") or "")[:10] or None)
    else:
        env.absent(act, "job_posting", "not_available", "No job postings were found on the verified website.", [ev_home])
    if x.get("news"):
        for item in x["news"]:
            ev = _page_evidence(env, site, item["source_url"], item.get("title"), item["method"])
            env.claim(act, "news_item", {"title": item.get("title"), "url": item.get("url")}, [ev], key=f"news.{(item.get('url') or '').lower()}",
                      confidence=conf, effective_date=item.get("date"), date_kind=item.get("date_kind") or ("published" if item.get("date") else None))
    else:
        env.absent(act, "news_item", "not_available", "No dated company news was found on the verified website.", [ev_home])


def add_nav_claims(env: Envelope, ads: list[dict] | None, nav_status: dict) -> None:
    """Confirmed NAV job ads (employer.orgnr equals the company or one of its registered workplaces)."""
    act = "hiring_and_activity"
    if ads is None:
        if nav_status.get("loaded"):
            ev = env.add_evidence(source_url="https://pam-stilling-feed.nav.no/api/v1/feed", source_class="official_job_feed",
                                  retrieved_at=nav_status.get("retrieved_at") or env.started_at, content_sha256=None,
                                  span=f"No active ad with this employer among {nav_status.get('ads')} ads changed in the last {nav_status.get('days')} days",
                                  method="nav_feed_scan")
            env.absent(act, "job_posting", "not_available", f"No active NAV job ad for this organisation in the last {nav_status.get('days')} days of the official feed.", [ev])
        else:
            env.absent(act, "job_posting", "blocked" if nav_status.get("error", "").startswith("stopped") else "failed",
                       f"NAV job feed unavailable in this run ({nav_status.get('error')}).")
        return
    for ad in ads:
        ev = env.add_evidence(source_url=ad["ad_url"], source_class="official_job_feed", retrieved_at=ad["retrieved_at"], content_sha256=ad["sha256"],
                              span=f"employer.orgnr={ad['employer_orgnr']}; employer.name={ad['employer_name']}; title={ad['title']}",
                              method="nav_feedentry_employer_orgnr_match", extra={"api_url": ad["detail_url"], "original_source_url": ad.get("source_url")})
        value = {"title": ad["title"], "url": ad["ad_url"], "location": ad.get("location"), "employment_type": ad.get("engagement"),
                 "extent": ad.get("extent"), "positions": ad.get("positions"), "occupations": ad.get("occupations") or None,
                 "application_due": ad.get("application_due"), "employer_unit": ad["employer_name"] if ad["employer_orgnr"] != env.org else None}
        env.claim(act, "job_posting", {k: v for k, v in value.items() if v}, [ev], key=f"job.nav.{ad['uuid']}", confidence=0.99,
                  effective_date=ad.get("published"), date_kind="published", valid_through=ad.get("expires"), source="NAV arbeidsplassen")
    first = ads[0]
    ev = env.add_evidence(source_url=first["ad_url"], source_class="official_job_feed", retrieved_at=first["retrieved_at"], content_sha256=first["sha256"],
                          span=f"{len(ads)} active ads with employer.orgnr in this organisation", method="nav_feedentry_employer_orgnr_match")
    env.claim(act, "hiring_status", {"active_job_ads": len(ads), "source": "NAV arbeidsplassen"}, [ev], confidence=0.99,
              effective_date=max((a.get("published") or "") for a in ads) or None)
    desc = next((a["employer_description"] for a in ads if a.get("employer_description")), None)
    if desc:
        ev = env.add_evidence(source_url=first["ad_url"], source_class="official_job_feed", retrieved_at=first["retrieved_at"],
                              content_sha256=first["sha256"], span=desc[:200], method="nav_employer_description")
        env.claim("legal_identity_and_brand", "employer_self_description", desc, [ev], confidence=0.95, note="Employer-written text in an official NAV job ad.")


def add_wikidata_claims(env: Envelope, rec: dict | None) -> None:
    if not rec:
        return
    web = "website_and_profiles"
    ev = env.add_evidence(source_url=rec["item"], source_class="open_data_wikidata", retrieved_at=rec.get("retrieved_at") or env.started_at,
                          content_sha256=rec.get("sha256"), span=f"P2333 (Norwegian organisation number) = {env.org}", method="wikidata_sparql_p2333",
                          extra={"licence": "CC0"})
    env.claim("legal_identity_and_brand", "wikidata_entity", {"url": rec["item"], "labels": rec.get("labels") or None}, [ev], confidence=0.97,
              identity_basis="wikidata_item_states_this_organisation_number")
    for platform, url in (rec.get("profiles") or {}).items():
        env.claim(web, "social_profile", {"platform": platform, "url": url}, [ev], key=f"social.{platform}.{url.lower()}", confidence=0.95,
                  identity_basis="wikidata_item_states_this_organisation_number")
    for value in rec.get("employees") or []:
        env.claim("hiring_and_activity", "wikidata_employees", value, [ev], confidence=0.85, note="Employee count recorded on Wikidata (community-maintained).")
    if rec.get("article"):
        env.claim("hiring_and_activity", "wikipedia_article", rec["article"], [ev], confidence=0.97)


def add_directory_claims(env: Envelope, page: dict | None) -> None:
    if not page or page.get("status") != "ok":
        return
    ev = env.add_evidence(source_url=page["url"], source_class="company_directory", retrieved_at=page["retrieved_at"], content_sha256=page.get("sha256"),
                          span=f"Organisasjonsnummer {env.org}; {page.get('name')}", method="directory_page_jsonld",
                          extra={"robots": "allowed (/bedrift/)"})
    env.claim("website_and_profiles", "directory_profile", {"platform": "fagfolkguiden", "url": page["url"]}, [ev], confidence=0.97,
              identity_basis="organisation_number_on_directory_page")
    if page.get("rating"):
        r = page["rating"]
        env.claim("hiring_and_activity", "review_summary", {"rating": r["value"], "scale": 5, "review_count": r["count"],
                                                            "provider": "Google rating as displayed on Fagfolkguiden"},
                  [ev], confidence=0.9, note="Third-party aggregate rating displayed on a directory page keyed by this organisation number.")


def add_workforce_claims(env: Envelope, result: dict | None, status: dict, has_registry_count: bool) -> None:
    act = "hiring_and_activity"
    if result and result.get("status") == "accepted":
        url = result["url"]
        period = {"start": f"{result['year']}-01-01", "end": f"{result['year']}-12-31"}
        ev = env.add_evidence(source_url=url, source_class="official_annual_account_copy", retrieved_at=result["retrieved_at"],
                              content_sha256=result["sha256"], span=result["span"], method=f"pdf_ocr_{result.get('backend')}",
                              reporting_period=period, extra={"page": result.get("page"), "content_fingerprint": result.get("fingerprint"),
                                                             "fingerprint_note": "sha256 of the PDF without its per-download trailer /ID"})
        label = "full-time equivalents (årsverk)" if result["measure"] == "full_time_equivalents" else "employees"
        env.claim(act, "workforce", {"value": result["value"], "measure": result["measure"], "label": label, "year": result["year"]},
                  [ev], key=f"workforce.{result['year']}", confidence=0.95, reporting_period=period,
                  note="Stated in the official annual accounts filed with Regnskapsregisteret; read with OCR from the filing copy.")
        return
    if result is None:
        reason = status.get("skipped_reason") or "The annual-account copy was not processed within this run's time/request budget."
        state = "blocked"
    elif result.get("status") in ("no_statement", "unreadable_pdf"):
        reason, state = "The filed annual accounts contain no explicit company-level workforce statement that OCR could read.", "not_available"
    elif result.get("status") == "not_found":
        reason, state = "No annual-account copy is available for the latest filing year.", "not_available"
    else:
        reason, state = f"Annual-account copy could not be processed ({result.get('status')}).", "failed"
    if not has_registry_count:
        env.absent(act, "workforce", state, reason)


def add_registry_activity(env: Envelope, entity: dict, roles: list[dict] | None, filings) -> None:
    """Dated official events (exact identity by construction)."""
    act = "hiring_and_activity"
    ev = next((c["evidence_ids"][0] for c in env.claims if c["field"] == "legal_name"), None)
    events = []
    for field, kind, label in (("founded_at", "founded", "Company founded (stiftelsesdato)"),
                               ("registered_at", "registered", "Registered in Enhetsregisteret"),
                               ("share_capital_registered_at", "share_capital_registered", "Current share capital registered"),
                               ("articles_dated", "articles_of_association", "Articles of association dated"),
                               ("employees_registered_at", "employee_count_updated", "Registered employee count updated"),
                               ("bankrupt_at", "bankruptcy", "Bankruptcy registered"),
                               ("liquidating_at", "liquidation", "Liquidation registered")):
        if entity.get(field):
            events.append((kind, entity[field], label, ev))
    role_ev = next((c["evidence_ids"][0] for c in env.claims if c["section"] == "leadership_and_workplaces" and c["evidence_ids"]), None)
    for group, changed in sorted({(r["group"], r["group_last_changed"]) for r in roles or [] if r.get("group_last_changed")}, key=lambda g: g[1], reverse=True):
        events.append(("role_group_changed", changed, f"Registered roles changed: {group}", role_ev))
    acc_ev = next((c["evidence_ids"][0] for c in env.claims if c["section"] == "annual_accounts" and c["evidence_ids"]), None)
    for filing in filings or []:
        events.append(("annual_accounts_period_end", filing["period_end"], f"Annual accounts for {filing['year']} on file", acc_ev))
    for kind, date, label, evid in events:
        if evid and date:
            env.claim(act, "registry_event", {"event": kind, "description": label}, [evid], key=f"event.{kind}.{date}.{label.lower()}",
                      effective_date=date[:10], date_kind="official_register_date")
