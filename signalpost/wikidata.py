"""Wikidata: entities carrying the Norwegian organisation number (P2333). One SPARQL request covers the whole batch.

Identity is exact by construction (the item states this organisation number). Wikidata is CC0 open data.
"""
from __future__ import annotations

import json
from .http import Http

ENDPOINT = "https://query.wikidata.org/sparql"
QUERY = """SELECT ?item ?org ?label ?site ?li ?fb ?ig ?x ?yt ?tt ?emp ?inception ?article WHERE {
  VALUES ?org { %s }
  ?item wdt:P2333 ?org .
  OPTIONAL { ?item rdfs:label ?label FILTER(LANG(?label) IN ("nb", "no", "en")) }
  OPTIONAL { ?item wdt:P856 ?site }
  OPTIONAL { ?item wdt:P4264 ?li }
  OPTIONAL { ?item wdt:P2013 ?fb }
  OPTIONAL { ?item wdt:P2003 ?ig }
  OPTIONAL { ?item wdt:P2002 ?x }
  OPTIONAL { ?item wdt:P2397 ?yt }
  OPTIONAL { ?item wdt:P7085 ?tt }
  OPTIONAL { ?item wdt:P1128 ?emp }
  OPTIONAL { ?item wdt:P571 ?inception }
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://no.wikipedia.org/> }
}"""
PROFILE_URL = {
    "li": ("linkedin", "https://www.linkedin.com/company/{}"),
    "fb": ("facebook", "https://www.facebook.com/{}"),
    "ig": ("instagram", "https://www.instagram.com/{}"),
    "x": ("x", "https://x.com/{}"),
    "yt": ("youtube", "https://www.youtube.com/channel/{}"),
    "tt": ("tiktok", "https://www.tiktok.com/@{}"),
}


def fetch_wikidata(http: Http, orgs: list[str], chunk: int = 1500) -> tuple[dict[str, dict], dict]:
    out: dict[str, dict] = {}
    meta = {"requests": 0, "error": None}
    for start in range(0, len(orgs), chunk):
        values = " ".join(f'"{o}"' for o in orgs[start:start + chunk])
        res = http.post_form(ENDPOINT, "open_data_wikidata", {"query": QUERY % values}, accept="application/sparql-results+json")
        meta["requests"] += 1
        meta["retrieved_at"] = res.retrieved_at
        if not res.ok:
            meta["error"] = res.error or f"http_{res.status}"
            continue
        try:
            rows = json.loads(res.body.decode("utf-8"))["results"]["bindings"]
        except (ValueError, KeyError):
            meta["error"] = "parse_error"
            continue
        for row in rows:
            org = row["org"]["value"]
            rec = out.setdefault(org, {"item": row["item"]["value"], "labels": set(), "sites": set(), "profiles": {}, "employees": set(),
                                       "inception": None, "article": None, "sha256": res.sha256, "retrieved_at": res.retrieved_at})
            if "label" in row:
                rec["labels"].add(row["label"]["value"])
            if "site" in row:
                rec["sites"].add(row["site"]["value"])
            for key, (platform, template) in PROFILE_URL.items():
                if key in row:
                    rec["profiles"][platform] = template.format(row[key]["value"].lstrip("@"))
            if "emp" in row:
                try:
                    rec["employees"].add(int(float(row["emp"]["value"])))
                except ValueError:
                    pass
            if "inception" in row:
                rec["inception"] = row["inception"]["value"][:10]
            if "article" in row:
                rec["article"] = row["article"]["value"]
    for rec in out.values():
        rec["labels"] = sorted(rec["labels"])
        rec["sites"] = sorted(rec["sites"])
        rec["employees"] = sorted(rec["employees"])
    return out, meta
