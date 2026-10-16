"""Fagfolkguiden company directory: pages keyed by organisation number (/bedrift/<slug>-<orgnr>).

The site's own sitemaps list every company page, so a handful of requests tells us which companies in the batch have a
page; only those pages are fetched. robots.txt allows /bedrift/ (only /api/, /admin/ etc. are disallowed).
The page must show the exact organisation number before anything is used. The aggregate rating is a third-party
(Google) rating displayed on the directory and is labelled as such.
"""
from __future__ import annotations

import json
import re

from .http import Http

BASE = "https://www.fagfolkguiden.no"
SITEMAP_INDEX = BASE + "/sitemap.xml"


def sitemap_hits(http: Http, wanted: set[str], max_maps: int = 12) -> tuple[dict[str, str], dict]:
    meta = {"requests": 0, "error": None, "maps": 0}
    if not http.allowed(BASE + "/bedrift/x"):
        meta["error"] = "robots_disallowed"
        return {}, meta
    res = http.get(SITEMAP_INDEX, "directory_sitemap", timeout=30)
    meta["requests"] += 1
    maps = re.findall(r"<loc>\s*([^<\s]+/sitemaps/[^<\s]+\.xml)\s*</loc>", res.text()) if res.ok else []
    if not maps:
        maps = [f"{BASE}/sitemaps/{i}.xml" for i in range(0, 6)]
    hits: dict[str, str] = {}
    for url in maps[:max_maps]:
        if http.budget.past_soft_deadline():
            break
        page = http.get(url, "directory_sitemap", timeout=60, max_bytes=40_000_000)
        meta["requests"] += 1
        meta["maps"] += 1
        if not page.ok:
            continue
        for loc in re.findall(r"<loc>\s*([^<\s]+/bedrift/[^<\s]*?-(\d{9}))\s*</loc>", page.text()):
            if loc[1] in wanted:
                hits[loc[1]] = loc[0]
    meta["hits"] = len(hits)
    return hits, meta


def fetch_directory_page(http: Http, org: str, url: str) -> dict | None:
    res = http.get(url, "directory_page", timeout=20)
    if not res.ok:
        return {"status": "error", "http_status": res.status, "url": url, "retrieved_at": res.retrieved_at}
    html = res.text()
    text = re.sub(r"<[^>]+>", " ", html)
    if org not in re.sub(r"\D", "", text):
        return {"status": "identity_mismatch", "url": url, "retrieved_at": res.retrieved_at}
    data = {}
    for raw in re.findall(r'<script[^>]*ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        for item in obj if isinstance(obj, list) else [obj]:
            if isinstance(item, dict) and str((item.get("identifier") or {}).get("value", "")) == org:
                data = item
    rating = data.get("aggregateRating") or {}
    out = {"status": "ok", "url": res.final_url or url, "retrieved_at": res.retrieved_at, "sha256": res.sha256,
           "name": data.get("name"), "description": data.get("description"), "same_as": data.get("sameAs") or []}
    try:
        value, count = float(rating.get("ratingValue")), int(rating.get("ratingCount") or rating.get("reviewCount"))
        if 0 < value <= 5 and count > 0:
            out["rating"] = {"value": value, "count": count, "best": rating.get("bestRating", 5)}
    except (TypeError, ValueError):
        pass
    return out
