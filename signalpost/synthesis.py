"""Decision-useful profile summary built only from published claims (every sentence cites evidence)."""
from __future__ import annotations

from .claims import Envelope


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
    parts = [f"{name['value']} is a Norwegian {form['value']['label'].lower() if form and form['value'].get('label') else 'entity'}"]
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
    if brand and brand["value"] and brand["value"].lower() not in name["value"].lower():
        say(f"It presents itself publicly as \"{brand['value']}\".", [brand], "identity")
    if activity:
        say(f"Registered business activity: {activity['value'][:240].rstrip('.')}.", [activity], "what_it_does")
    if desc:
        say(f"Its own website describes it as: \"{desc['value'][:240].rstrip('.')}\" (company-reported).", [desc], "what_it_does")

    # Size and finances ------------------------------------------------------------
    employees = _first(env, "registered_employees")
    if employees:
        say(f"The registry lists {employees['value']} employees.", [employees], "size")
    revenue = sorted(_available(env, "revenue", "annual_accounts"), key=lambda c: c["reporting_period"]["end"], reverse=True)
    result = sorted(_available(env, "net_result", "annual_accounts"), key=lambda c: c["reporting_period"]["end"], reverse=True)
    equity = sorted(_available(env, "equity", "annual_accounts"), key=lambda c: c["reporting_period"]["end"], reverse=True)
    if revenue:
        latest = revenue[0]
        year = latest["reporting_period"]["end"][:4]
        text = f"In the {year} filed accounts, operating revenue was {money(latest['value'], latest.get('unit') or 'NOK')}"
        used = [latest]
        if len(revenue) > 1 and revenue[1]["value"]:
            prev = revenue[1]
            change = (latest["value"] - prev["value"]) / abs(prev["value"]) * 100
            text += f" ({'up' if change >= 0 else 'down'} {abs(change):.0f}% from {money(prev['value'], prev.get('unit') or 'NOK')} in {prev['reporting_period']['end'][:4]})"
            used.append(prev)
        if result and result[0]["reporting_period"]["end"][:4] == year:
            text += f", with a net result of {money(result[0]['value'], result[0].get('unit') or 'NOK')}"
            used.append(result[0])
        if equity and equity[0]["reporting_period"]["end"][:4] == year:
            text += f" and equity of {money(equity[0]['value'], equity[0].get('unit') or 'NOK')}"
            used.append(equity[0])
        say(text + ".", used, "finances")
    elif result:
        latest = result[0]
        say(f"The {latest['reporting_period']['end'][:4]} accounts report no operating revenue line; net result was {money(latest['value'], latest.get('unit') or 'NOK')}.", [latest], "finances")
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
    if changes:
        statements.append({"topic": "changes", "text": f"Since the previous run: {len(changes)} change(s), {len(material)} material "
                                                        f"({', '.join(sorted({c['change_type'] for c in changes}))}).", "evidence_ids": []})
    unknowns_text = " ".join(unknowns)
    return {
        "headline": statements[0]["text"] if statements else None,
        "statements": statements,
        "unknowns": unknowns,
        "text": " ".join(s["text"] for s in statements) + (f" Unknown: {unknowns_text}" if unknowns else ""),
        "method": "deterministic template over published claims; no unsupported statements",
    }
