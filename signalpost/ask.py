"""Deterministic research agent over emitted envelopes: cited Q&A, cross-company screens, honest abstention.

python -m signalpost.ask --envelopes out/envelopes.jsonl --org 979543883 --question "Who leads it and how are the numbers?"
python -m signalpost.ask --envelopes out/envelopes.jsonl --screen "companies in Oslo with more than 5 employees top 10 by revenue"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

UNSUPPORTED = re.compile(r"glassdoor|sentiment|buzz|loudest|traffic|velocity|culture|fraud|popular|reputation|happiest|valuation|stock price|share price|"
                         r"web\s*traffic|no website|without (a )?website|ranking of reviews|review popularity", re.I)
TOPICS = {
    "finance": ("financial", "finance", "account", "revenue", "turnover", "income", "profit", "result", "debt", "asset", "equity", "regnskap", "omsetning", "numbers", "filing"),
    "leadership": ("lead", "ceo", "chair", "board", "role", "who runs", "manager", "owner", "daglig", "styre"),
    "locations": ("location", "where", "address", "office", "workplace", "subunit", "operate", "site"),
    "web": ("website", "social", "linkedin", "facebook", "instagram", "profile", "online", "web"),
    "hiring": ("hiring", "job", "vacanc", "recruit", "work here", "working here", "employees", "staff", "workforce", "årsverk", "arsverk"),
    "activity": ("news", "activity", "update", "recent", "changed", "change", "event"),
    "about": ("what does", "what do", "business", "about", "describe", "activity"),
}
SECTION_FOR = {"finance": {"annual_accounts"}, "leadership": {"leadership_and_workplaces"}, "locations": {"leadership_and_workplaces"},
               "web": {"website_and_profiles"}, "hiring": {"hiring_and_activity"}, "activity": {"hiring_and_activity"},
               "about": {"legal_identity_and_brand"}}
FIELDS_FOR = {"leadership": {"ceo", "board_chair", "board_member", "board_deputy_chair", "owner", "general_partner", "auditor", "accountant", "leader_named_on_website"},
              "locations": {"registered_workplace", "business_address", "website_address"},
              "hiring": {"job_posting", "hiring_status", "careers_page", "workforce", "registered_employees", "wikidata_employees"},
              "activity": {"news_item", "news_page", "registry_event", "wikipedia_article"}}


def load(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _cite(claim: dict, evidence: dict) -> dict:
    ev = [evidence[e] for e in claim.get("evidence_ids", []) if e in evidence]
    first = ev[0] if ev else {}
    return {"claim": claim["field"], "key": claim.get("key"), "value": claim.get("value"), "reporting_period": claim.get("reporting_period"),
            "effective_date": claim.get("effective_date"), "source_url": first.get("source_url"), "retrieved_at": first.get("retrieved_at"),
            "content_sha256": first.get("content_sha256"), "source_class": first.get("source_class"), "evidence_span": first.get("claim_span")}


def answer(envelope: dict, question: str) -> dict:
    q = question.lower()
    if UNSUPPORTED.search(q):
        return {"organisation_number": envelope.get("organisation_number"), "question": question, "abstained": True, "facts": [],
                "reason": "The question asks for a signal this agent does not collect from permitted sources."}
    topics = [t for t, words in TOPICS.items() if any(w in q for w in words)] or list(TOPICS)
    evidence = {e["id"]: e for e in envelope.get("evidence", [])}
    facts, gaps = [], []
    for claim in envelope.get("claims", []):
        relevant = any(
            (claim["field"] in FIELDS_FOR.get(t, set())) or (t not in FIELDS_FOR and claim["section"] in SECTION_FOR[t])
            for t in topics)
        if not relevant:
            continue
        if claim["availability"] == "available":
            if claim["section"] == "annual_accounts" and "finance" in topics:
                latest = max((c.get("reporting_period", {}).get("end", "") for c in envelope["claims"] if c["section"] == "annual_accounts" and c.get("reporting_period")), default="")
                if claim.get("reporting_period", {}).get("end") != latest:
                    continue
            facts.append(_cite(claim, evidence))
        else:
            gaps.append({"claim": claim["field"], "availability": claim["availability"], "reason": claim.get("reason")})
    summary = [s["text"] for s in (envelope.get("summary") or {}).get("statements", [])
               if any(t in s.get("topic", "") or s.get("topic") in {"identity"} for t in topics)]
    return {"organisation_number": envelope.get("organisation_number"), "legal_name": envelope.get("legal_name"), "question": question,
            "topics": topics, "abstained": False, "answer": " ".join(summary[:4]) or None, "facts": facts[:60], "not_available": gaps[:20],
            "policy": "Answers only from published claims with sources; missing evidence stays missing."}


# ------------------------------------------------------------------------------------------- screening

_NUM = r"(?:nok\s*)?(\d+(?:[.,]\d+)?)\s*(million|mill|mnok|m\b|billion|mrd|bn|k\b|thousand)?"


def _amount(value: str, unit: str | None) -> float:
    number = float(value.replace(",", "."))
    unit = (unit or "").lower()
    return number * (1e9 if unit in ("billion", "mrd", "bn") else 1e6 if unit in ("million", "mill", "mnok", "m") else 1e3 if unit in ("k", "thousand") else 1)


def plan_screen(query: str) -> dict:
    q = query.lower().strip()
    filters = []
    city = re.search(r"\b(?:in|i|located in|based in)\s+([a-zæøå][a-zæøå\- ]+?)(?=\s+(?:with|that|and|having|med|top|sorted|by)\b|$)", q)
    if city:
        filters.append({"field": "municipality", "operator": "eq", "value": city.group(1).strip().upper()})
    ops = [(r"(?:more than|over|above|greater than|>)\s*", ">"), (r"(?:at least|minimum|>=)\s*", ">="), (r"(?:fewer than|less than|under|below|<)\s*", "<"),
           (r"(?:at most|maximum|<=)\s*", "<=")]
    for metric, pattern in (("employees", r"(?:registered\s+)?(?:employees|ansatte|staff)"), ("revenue", r"(?:revenue|turnover|omsetning|sales)")):
        for words, op in ops:
            m = re.search(pattern + r"\s*(?:of\s*)?" + words + _NUM, q) or re.search(words + _NUM + r"\s*(?:registered\s+)?" + pattern, q)
            if m:
                value = _amount(m.group(1), m.group(2)) if metric == "revenue" else float(m.group(1).replace(",", "."))
                filters.append({"field": metric, "operator": op, "value": int(value) if value.is_integer() else value})
                break
    form = re.search(r"(?:legal form|organisation form|organization form|form)\s*(?:=|is|:)?\s*([a-zæøå]{2,4})\b", q)
    if form:
        filters.append({"field": "legal_form", "operator": "eq", "value": form.group(1).upper()})
    if re.search(r"\b(with|having|has) (a )?(verified )?website\b", q):
        filters.append({"field": "website", "operator": "exists", "value": True})
    if re.search(r"\bhiring\b|\bjob (ads|postings)\b|open roles", q):
        filters.append({"field": "hiring", "operator": "exists", "value": True})
    sort = re.search(r"(?:top|largest|biggest)\s*(\d+)?\s*(?:by|on)?\s*(revenue|employees|equity|result|profit)?", q)
    order = None
    if sort and (sort.group(2) or "top" in q):
        order = {"field": {"profit": "net_result", "result": "net_result"}.get(sort.group(2) or "revenue", sort.group(2) or "revenue"),
                 "direction": "desc", "limit": int(sort.group(1)) if sort.group(1) else None}
    return {"query": query, "filters": filters, "order": order}


def _metrics(envelope: dict) -> dict:
    out = {"municipality": None, "employees": None, "revenue": None, "equity": None, "net_result": None, "legal_form": None, "website": False, "hiring": False}
    latest = {}
    cites = {}
    evidence = {e["id"]: e for e in envelope.get("evidence", [])}
    for c in envelope.get("claims", []):
        if c["availability"] != "available":
            continue
        f = c["field"]
        if f == "business_address":
            out["municipality"] = (c["value"].get("municipality") or c["value"].get("city") or "").upper() or None
            cites["municipality"] = _cite(c, evidence)
        elif f == "registered_employees":
            out["employees"] = c["value"]
            cites["employees"] = _cite(c, evidence)
        elif f == "legal_form":
            out["legal_form"] = c["value"].get("code")
            cites["legal_form"] = _cite(c, evidence)
        elif f == "official_website":
            out["website"] = True
            cites["website"] = _cite(c, evidence)
        elif f in ("job_posting", "hiring_status"):
            out["hiring"] = True
            cites["hiring"] = _cite(c, evidence)
        elif f in ("revenue", "equity", "net_result") and (c.get("account_type") or "SELSKAP") == "SELSKAP":
            end = c["reporting_period"]["end"]
            if end >= latest.get(f, ""):
                latest[f] = end
                out[f] = c["value"]
                cites[f] = _cite(c, evidence)
    return out, cites


def screen(envelopes: list[dict], query: str) -> dict:
    if UNSUPPORTED.search(query):
        return {"query": query, "abstained": True, "plan": None, "results": [],
                "reason": "The screen needs a signal this agent does not collect from permitted sources."}
    plan = plan_screen(query)
    if not plan["filters"] and not plan["order"]:
        return {"query": query, "abstained": True, "plan": plan, "results": [], "reason": "No supported filter or ranking was recognised."}
    rows = []
    for env in envelopes:
        values, cites = _metrics(env)
        ok = True
        for f in plan["filters"]:
            v = values.get(f["field"])
            if f["operator"] == "exists":
                ok = ok and bool(v)
            elif v is None:
                ok = False  # unknown never passes a numeric/equality filter
            elif f["operator"] == "eq":
                ok = ok and str(v).upper() == str(f["value"]).upper()
            else:
                ok = ok and {">": v > f["value"], ">=": v >= f["value"], "<": v < f["value"], "<=": v <= f["value"]}[f["operator"]]
        if ok:
            fields = [f["field"] for f in plan["filters"]] + ([plan["order"]["field"]] if plan["order"] else [])
            rows.append({"organisation_number": env["organisation_number"], "legal_name": env.get("legal_name"),
                         "values": {k: values.get(k) for k in fields}, "citations": [cites[k] for k in fields if k in cites]})
    if plan["order"]:
        key = plan["order"]["field"]
        rows = [r for r in rows if r["values"].get(key) is not None]
        rows.sort(key=lambda r: (-r["values"][key], r["organisation_number"]))
        if plan["order"]["limit"]:
            rows = rows[:plan["order"]["limit"]]
    return {"query": query, "abstained": False, "plan": plan, "results": rows,
            "note": "Companies without a value for a filtered field are excluded, never treated as zero."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="signalpost.ask", description="Ask cited questions or screen companies from envelopes.jsonl")
    parser.add_argument("--envelopes", required=True)
    parser.add_argument("--org")
    parser.add_argument("--question")
    parser.add_argument("--screen")
    parser.add_argument("--history", help="Append this question/screen and its answer to a JSON history file")
    args = parser.parse_args(argv)
    envelopes = load(args.envelopes)
    if args.screen:
        result = screen(envelopes, args.screen)
    elif args.org and args.question:
        env = next((e for e in envelopes if e.get("organisation_number") == args.org), None)
        if env is None:
            print(json.dumps({"error": "organisation number not in envelopes"}))
            return 2
        result = answer(env, args.question)
    else:
        parser.error("use --screen, or --org with --question")
    if args.history:
        path = Path(args.history)
        history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"history": []}
        history["history"].append({"at": datetime.now(timezone.utc).isoformat(), "result": result})
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
