"""NAV Job Vacancy Feed (pam-stilling-feed): official Norwegian job ads with the employer's organisation number.

Docs: https://navikt.github.io/pam-stilling-feed/ (free; public token published by NAV; terms of use apply).
Strategy: read the feed from If-Modified-Since = now - N days (one request per 1,000 ad changes), keep the latest state of
each ad, pre-match employer names to the batch, then confirm every candidate with the ad detail whose employer.orgnr must
equal the company or one of its registered workplaces. Only confirmed ads are published.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from .http import Http
from .website import fold

BASE = "https://pam-stilling-feed.nav.no"
TOKEN_URL = BASE + "/api/publicToken"
FEED_URL = BASE + "/api/v1/feed"
SUFFIX = re.compile(r"\b(as|asa|ans|da|sa|ba|enk|nuf|ks|avd|avdeling|hf|iks|kf)\b")


def name_key(name: str) -> str:
    text = fold(name or "")
    text = SUFFIX.sub(" ", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


class NavJobs:
    def __init__(self, http: Http, days: int = 40, max_pages: int = 160, log=print):
        self.http = http
        self.days = days
        self.max_pages = max_pages
        self.log = log
        self.token = None
        self.ads: dict[str, dict] = {}  # uuid -> latest feed item
        self.pages = 0
        self.error = None
        self.retrieved_at = None
        self.feed_sha = []

    def _auth_get(self, url: str, extra: dict | None = None, timeout: float = 30.0):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if extra:
            headers.update(extra)
        # Http.get does not take headers; temporarily use the session with budget accounting via a manual call.
        return self.http.get_with_headers(url, "official_jobs_feed", headers, timeout=timeout)

    def load(self) -> None:
        res = self.http.get(TOKEN_URL, "official_jobs_feed", timeout=15)
        if not res.ok:
            self.error = f"token_unavailable:{res.status or res.error}"
            return
        match = re.search(r"(eyJ[\w-]+\.[\w-]+\.[\w-]+)", res.text())
        if not match:
            self.error = "token_not_found"
            return
        self.token = match.group(1)
        since = datetime.now(timezone.utc) - timedelta(days=self.days)
        url = FEED_URL
        headers = {"If-Modified-Since": format_datetime(since, usegmt=True)}
        while url and self.pages < self.max_pages:
            if self.http.budget.past_soft_deadline() or self.http.budget.remaining() < 50:
                self.error = "stopped_early_budget"
                break
            res = self._auth_get(url, headers if self.pages == 0 else None)
            self.pages += 1
            if not res.ok:
                self.error = f"feed_http_{res.status or res.error}"
                break
            try:
                page = res.json()
            except ValueError:
                self.error = "feed_parse_error"
                break
            self.retrieved_at = res.retrieved_at
            self.feed_sha.append(res.sha256)
            for item in page.get("items") or []:
                entry = item.get("_feed_entry") or {}
                uuid = entry.get("uuid") or item.get("id")
                if uuid:
                    self.ads[uuid] = {"uuid": uuid, "entry_id": item.get("id"), "status": entry.get("status"), "title": entry.get("title") or item.get("title"),
                                      "business": entry.get("businessName"), "municipal": entry.get("municipal"),
                                      "modified": item.get("date_modified"), "feed_url": BASE + (page.get("feed_url") or "")}
            next_url = page.get("next_url")
            if not next_url or not page.get("items"):
                break
            url = BASE + next_url
        active = sum(1 for a in self.ads.values() if a["status"] == "ACTIVE")
        self.log(f"nav: {self.pages} feed pages, {len(self.ads)} ads, {active} active")

    def candidates(self, names_by_org: dict[str, set[str]]) -> dict[str, list[dict]]:
        """Pre-match active ads to organisations by normalised employer name (confirmation happens later)."""
        index: dict[str, set[str]] = {}
        for org, names in names_by_org.items():
            for name in names:
                key = name_key(name)
                if len(key) >= 4:
                    index.setdefault(key, set()).add(org)
        out: dict[str, list[dict]] = {}
        for ad in self.ads.values():
            if ad["status"] != "ACTIVE" or not ad.get("business"):
                continue
            key = name_key(ad["business"])
            orgs = set(index.get(key, set()))
            # "Company AS avd Bergen" style business names: try the prefix before a dash/comma/"avd".
            if not orgs:
                head = re.split(r"\s[-–,(]|\bavd\b|\bavdeling\b", fold(ad["business"]))[0]
                orgs = set(index.get(name_key(head), set()))
            for org in orgs:
                out.setdefault(org, []).append(ad)
        return out

    def confirm(self, ad: dict, allowed_orgs: set[str]) -> dict | None:
        res = self._auth_get(BASE + f"/api/v1/feedentry/{ad['entry_id']}")
        if not res.ok:
            return None
        try:
            body = res.json()
        except ValueError:
            return None
        content = body.get("ad_content") or {}
        employer = content.get("employer") or {}
        orgnr = str(employer.get("orgnr") or "")
        if orgnr not in allowed_orgs or body.get("status", "ACTIVE") != "ACTIVE":
            return None
        locations = content.get("workLocations") or []
        place = ", ".join(filter(None, [(locations[0].get("city") or locations[0].get("municipal") or "").title(),
                                        (locations[0].get("county") or "").title()])) if locations else None
        return {
            "uuid": ad["uuid"], "title": content.get("title") or ad.get("title"), "employer_name": employer.get("name"),
            "employer_orgnr": orgnr, "employer_homepage": employer.get("homepage"), "employer_description": (employer.get("description") or "")[:800] or None,
            "published": (content.get("published") or "")[:10] or None, "expires": (content.get("expires") or "")[:10] or None,
            "updated": (content.get("updated") or "")[:10] or None, "application_due": content.get("applicationDue"),
            "positions": content.get("positioncount"), "engagement": content.get("engagementtype"), "extent": content.get("extent"),
            "location": place, "ad_url": content.get("link") or f"https://arbeidsplassen.nav.no/stillinger/stilling/{ad['uuid']}",
            "source_url": content.get("sourceurl"), "detail_url": BASE + f"/api/v1/feedentry/{ad['entry_id']}",
            "retrieved_at": res.retrieved_at, "sha256": res.sha256,
            "occupations": [c.get("name") for c in (content.get("categoryList") or []) if c.get("categoryType") == "STYRK08"][:3],
        }
