"""Decision-useful profile summary built only from published claims (every sentence cites evidence)."""
from __future__ import annotations

from datetime import date

from .claims import Envelope, fiscal_label


def _days(period: dict) -> int:
    try:
        return (date.fromisoformat(period["end"]) - date.fromisoformat(period["start"])).days
    except (KeyError, ValueError):
        return -1


def money(value: float, currency: str = "NOK") -> str:
    sign = "-" if value < 0 else ""
    v = abs(value)
    if v >= 1e9:
        text = f"{v / 1e9:.1f} billion"
    elif v >= 1e6:
        text = f"{v / 1e6:.1f} million"
    elif v >= 1e3:
        text = f"{v / 1e3:.0f} thousand"
    else:
        text = f"{v:.0f}"
    return f"{currency} {sign}{text}"


def _available(env: Envelope, field: str, section: str | None = None) -> list[dict]:
    return [c for c in env.claims if c["field"] == field and c["availability"] == "available" and (section is None or c["section"] == section)]


def _first(env: Envelope, field: str):
    items = _available(env, field)
    return items[0] if items else None


def build_summary(env: Envelope, changes: list[dict]) -> dict:
    statements: list[dict] = []
    unknowns: list[str] = []

    def say(text: str, claims: list[dict], kind: str):
        ids = sorted({e for c in claims if c for e in c["evidence_ids"]})
        if ids:
            statements.append({"topic": kind, "text": text, "evidence_ids": ids})

    name = _first(env, "legal_name")
    form = _first(env, "legal_form")
    industry = _first(env, "industry")
    address = _first(env, "business_address")
    status = _first(env, "registry_status")
    brand = _first(env, "public_brand_name")
    desc = _first(env, "website_description")
    activity = _first(env, "registered_activity") or _first(env, "statutory_purpose")
    founded = _first(env, "founded_date")

    # What it does --------------------------------------------------------------
    display = name["value"] if name else f"Organisation {env.org}"
    parts = [f"{display} is a Norwegian {form['value']['label'].lower() if form and form['value'].get('label') else 'entity'}"]
    if address and address["value"].get("city"):
        parts.append(f"registered in {address['value'].get('municipality') or address['value']['city']}")
    if industry:
        parts.append(f"classified under NACE {industry['value']['nace']} ({industry['value']['label']})")
    sentence = " ".join(parts[:1]) + (", " + ", ".join(parts[1:]) if len(parts) > 1 else "") + "."
    if founded:
        sentence = sentence[:-1] + f", founded {founded['value']}."
    say(sentence, [name, form, industry, address, founded], "identity")
    if status and status["value"] != "active":
        say(f"Registry status: {status['value'].replace('_', ' ')}.", [status], "identity")
    if brand and brand["value"] and brand["value"].lower() not in display.lower():
        say(f"It presents itself publicly as \"{brand['value']}\".", [brand], "identity")
    if activity:
        say(f"Registered business activity: {activity['value'][:240].rstrip('.')}.", [activity], "what_it_does")
    if desc:
        say(f"Its own website describes it as: \"{desc['value'][:240].rstrip('.')}\" (company-reported).", [desc], "what_it_does")

    # Size and finances ------------------------------------------------------------
    employees = _first(env, "registered_employees")
    if employees:
        say(f"The registry lists {employees['value']} employees.", [employees], "size")
    filed = next((c for c in _available(env, "workforce") if c.get("key", "").startswith("workforce.") and c["key"] != "workforce.registry"), None)
    if filed:
        v = filed["value"]
        what = "full-time equivalents (årsverk)" if v["measure"] == "full_time_equivalents" else "employees"
        say(f"Its {v['year']} annual accounts state {v['value']} {what}" + (" — the company reports no staff." if v["value"] == 0 else "."), [filed], "size")
    # Company (SELSKAP) accounts only; group accounts are a different entity scope and are reported separately.
    def series(field: str, account: str = "SELSKAP") -> list[dict]:
        items = [c for c in _available(env, field, "annual_accounts") if (c.get("account_type") or "SELSKAP") == account]
        return sorted(items, key=lambda c: c["reporting_period"]["end"], reverse=True)

    def same_period(c: dict, ref: dict) -> dict | None:
        return next((x for x in c if x["reporting_period"]["end"] == ref["reporting_period"]["end"]), None)

    revenue, result, equity = series("revenue"), series("net_result"), series("equity")
    anchor = revenue[0] if revenue else (result[0] if result else None)
    if anchor:
        label = fiscal_label(anchor["reporting_period"])
        unit = anchor.get("unit") or "NOK"
        used = [anchor]
        if revenue:
            latest = revenue[0]
            # Compare with the previous filing only when it covers a period of the same length, older -> newer.
            # Only the immediately preceding filing, and only if it covers a period of the same length.
            before = next((r for r in revenue[1:] if r["reporting_period"]["end"] < latest["reporting_period"]["end"]), None)
            prev = before if before and abs(_days(before["reporting_period"]) - _days(latest["reporting_period"])) <= 3 else None
            if prev:
                direction = "rose" if latest["value"] > prev["value"] else "fell" if latest["value"] < prev["value"] else "was unchanged"
                text = (f"Operating revenue {direction} from {money(prev['value'], unit)} in {fiscal_label(prev['reporting_period'])} "
                        f"to {money(latest['value'], unit)} in {label}")
                if prev["value"] > 0 and latest["value"] > 0:
                    text += f" ({(latest['value'] - prev['value']) / prev['value'] * 100:+.0f}%)"
                used.append(prev)
            else:
                text = f"Operating revenue in {label} was {money(latest['value'], unit)}"
                if before:
                    text += " (the previous filing covers a period of a different length, so no growth rate is computed)"
        else:
            text = f"The {label} accounts report no operating revenue line"
        r, q = same_period(result, anchor), same_period(equity, anchor)
        if r:
            text += f"; net result was {money(r['value'], unit)}"
            used.append(r)
        if q:
            text += f" and equity {money(q['value'], unit)}"
            used.append(q)
        say(text + ".", used, "finances")
        group = series("revenue", "KONSERN")
        if group:
            g = group[0]
            say(f"Group (konsern) accounts are filed separately: group operating revenue in {fiscal_label(g['reporting_period'])} was "
                f"{money(g['value'], g.get('unit') or 'NOK')}.", [g], "finances")
    else:
        unknowns.append("Structured annual-account figures were not available from Regnskapsregisteret in this run.")

    # Leadership and locations -----------------------------------------------------
    ceo = _first(env, "ceo")
    chair = _first(env, "board_chair")
    lead = []
    if ceo and chair and ceo["value"]["name"] == chair["value"]["name"]:
        lead.append(f"{ceo['value']['name']} is registered as both CEO (daglig leder) and board chair")
    else:
        if ceo:
            lead.append(f"{ceo['value']['name']} is registered as CEO (daglig leder)")
        if chair:
            lead.append(f"{chair['value']['name']} chairs the board")
    board = len(_available(env, "board_member"))
    if lead:
        say("; ".join(lead) + (f"; the board has {board} further registered member(s)." if board else "."), [ceo, chair], "leadership")
    elif not _available(env, "owner"):
        unknowns.append("No CEO or board chair is registered.")
    owner = _first(env, "owner")
    if owner:
        say(f"{owner['value']['name']} is the registered owner (innehaver).", [owner], "leadership")
    workplaces = _available(env, "registered_workplace")
    if workplaces:
        cities = sorted({(w["value"].get("municipality") or "").title() for w in workplaces if w["value"].get("municipality")})
        say(f"It has {len(workplaces)} registered workplace(s)" + (f" in {', '.join(cities[:6])}" + (" and more" if len(cities) > 6 else "") if cities else "") + ".",
            workplaces[:5], "locations")

    # Web footprint --------------------------------------------------------------
    site = _first(env, "official_website")
    socials = _available(env, "social_profile")
    if site:
        level = {"exact": "its organisation number appears on the site", "strong": "registry contact data and name match the site",
                 "registry_declared": "the site is declared in the official registry"}[site["identity_level"]]
        say(f"Official website: {site['value']} (verified because {level}).", [site], "web")
    else:
        miss = next((c for c in env.claims if c["field"] == "official_website"), None)
        unknowns.append("No verified official website" + (f" ({miss.get('reason')})" if miss and miss.get("reason") else "") + ".")
    if socials:
        platforms = sorted({s["value"]["platform"] for s in socials})
        say(f"Company-owned profiles found on: {', '.join(platforms)}.", socials[:6], "web")

    # Hiring and activity -------------------------------------------------------------
    jobs = _available(env, "job_posting")
    careers = _first(env, "careers_page")
    ats = _available(env, "recruiting_system_link")
    if jobs:
        say(f"The company website lists {len(jobs)} job posting(s), suggesting active hiring.", jobs[:5], "hiring")
    elif careers or ats:
        say("The website has a careers section, but no individual postings were extracted.", [careers] + ats[:2], "hiring")
    else:
        unknowns.append("No current job postings were found from permitted sources.")
    news = [n for n in _available(env, "news_item") if n.get("effective_date")]
    if news:
        latest = max(news, key=lambda n: n["effective_date"])
        say(f"Most recent dated company-published item: \"{(latest['value'].get('title') or '')[:120]}\" ({latest['effective_date']}).", [latest], "activity")
    events = sorted(_available(env, "registry_event"), key=lambda e: e.get("effective_date") or "", reverse=True)
    recent = [e for e in events if e["value"]["event"] in ("role_group_changed", "share_capital_registered", "articles_of_association")]
    if recent:
        e = recent[0]
        say(f"Latest dated registry change: {e['value']['description'].lower()} on {e['effective_date']}.", [e], "activity")

    # Changes ---------------------------------------------------------------------
    material = [c for c in changes if c.get("material")]
    previous = getattr(env, "previous_run_id", None)
    if changes:
        ids = sorted({e for c in changes for e in (c.get("current_evidence_ids") or []) + (c.get("previous_evidence_ids") or [])})
        examples = "; ".join(f"{c['field'].replace('_', ' ')}: {str(c.get('previous_value'))[:40]} → {str(c.get('current_value'))[:40]}"
                             for c in material[:3])
        statements.append({"topic": "changes", "text": f"Since the previous run ({previous}): {len(changes)} change(s), {len(material)} material "
                                                        f"({', '.join(sorted({c['change_type'] for c in changes}))})" + (f" — {examples}." if examples else "."),
                           "evidence_ids": ids})
    elif previous:
        statements.append({"topic": "changes", "text": f"No changes since the previous run ({previous}); all earlier evidence remains valid.", "evidence_ids": []})
    else:
        statements.append({"topic": "changes", "text": "First observation of this company: no previous snapshot to compare against.", "evidence_ids": []})
    unknowns_text = " ".join(unknowns)
    return {
        "headline": statements[0]["text"] if statements else None,
        "statements": statements,
        "unknowns": unknowns,
        "text": " ".join(s["text"] for s in statements) + (f" Unknown: {unknowns_text}" if unknowns else ""),
        "method": "deterministic template over published claims; no unsupported statements",
    }
