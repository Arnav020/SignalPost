"""Deterministic extraction from company-owned pages: structured data first, DOM second, text rules last."""
from __future__ import annotations

import json
import re
import warnings
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

ORG_WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)


def valid_org_number(value: str) -> bool:
    if not re.fullmatch(r"\d{9}", value or ""):
        return False
    total = sum(int(d) * w for d, w in zip(value[:8], ORG_WEIGHTS))
    check = 11 - (total % 11)
    check = 0 if check == 11 else check
    return check != 10 and check == int(value[8])


# 9 digits optionally grouped 3-3-3 with spaces, dots or non-breaking spaces; optional NO prefix / MVA suffix.
_ORG_PATTERN = re.compile(r"(?<![\d])(?:NO\s?)?(\d{3})[\s. ]?(\d{3})[\s. ]?(\d{3})(?:\s?MVA)?(?![\d])")


def find_org_numbers(text: str) -> set[str]:
    found = set()
    for match in _ORG_PATTERN.finditer(text or ""):
        number = "".join(match.groups())
        if valid_org_number(number):
            found.add(number)
    return found


def org_context(text: str, org: str, width: int = 70) -> str | None:
    """Return the text span around an org-number mention (evidence span)."""
    spaced = rf"{org[:3]}[\s. ]?{org[3:6]}[\s. ]?{org[6:]}"
    match = re.search(spaced, text)
    if not match:
        return None
    start, end = max(0, match.start() - width), min(len(text), match.end() + width)
    return re.sub(r"\s+", " ", text[start:end]).strip()


SOCIAL_RULES = [
    ("linkedin", re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/(company|school|showcase)/([^/?#]+)", re.I)),
    ("facebook", re.compile(r"^https?://(www\.|m\.|web\.|nb-no\.)?facebook\.com/(?!sharer|share|dialog|plugins|tr\b|login|groups/?$|events/?$)([^?#]+)", re.I)),
    ("instagram", re.compile(r"^https?://(www\.)?instagram\.com/(?!p/|explore|accounts|reel/)([^/?#]+)", re.I)),
    ("youtube", re.compile(r"^https?://(www\.|m\.)?youtube\.com/((channel|c|user)/[^/?#]+|@[^/?#]+)", re.I)),
    ("x", re.compile(r"^https?://(www\.)?(twitter|x)\.com/(?!intent|share|home|search|hashtag)([^/?#]+)", re.I)),
    ("tiktok", re.compile(r"^https?://(www\.)?tiktok\.com/(@[^/?#]+)", re.I)),
    ("pinterest", re.compile(r"^https?://([a-z]{2}\.)?pinterest\.[a-z.]+/(?!pin/)([^/?#]+)", re.I)),
    ("vimeo", re.compile(r"^https?://(www\.)?vimeo\.com/(?!\d+$)([^/?#]+)", re.I)),
    ("github", re.compile(r"^https?://(www\.)?github\.com/([^/?#]+)/?$", re.I)),
    ("threads", re.compile(r"^https?://(www\.)?threads\.(net|com)/(@[^/?#]+)", re.I)),
    ("snapchat", re.compile(r"^https?://(www\.)?snapchat\.com/add/([^/?#]+)", re.I)),
]

_SOCIAL_BAD_TAIL = {"", "home", "pages", "people", "watch", "sharer.php", "share", "intent", "privacy", "help", "about", "legal", "policies"}

ATS_HOSTS = (
    "teamtailor.com", "webcruiter.no", "webcruiter.com", "jobbnorge.no", "recman.no", "recman.page", "easycruit.com",
    "hrmanager.no", "talentech.io", "reachmee.com", "jobylon.com", "workday", "smartrecruiters.com", "lever.co",
    "greenhouse.io", "varbi.com", "emply.com", "finn.no/job", "arbeidsplassen.nav.no", "career.", "karriere.",
    "jobs.", "bamboohr.com", "personio", "hrmanager", "simployer", "jobbsafari", "careers.",
)

LINK_KINDS = {
    "contact": ("kontakt", "contact", "finn-oss", "find-us", "kontakt-oss", "om-oss/kontakt"),
    "about": ("om-oss", "om_oss", "omoss", "about", "hvem-er-vi", "selskapet", "company", "historie", "om-firmaet", "bedriften"),
    "people": ("ansatte", "medarbeidere", "team", "people", "ledelse", "leadership", "management", "styret", "board", "vare-folk", "folka", "kontaktpersoner"),
    "careers": ("karriere", "career", "jobb", "jobs", "stilling", "ledige", "vacanc", "rekrutter", "join-us", "work-with-us", "jobbe-hos"),
    "news": ("nyheter", "news", "aktuelt", "presse", "press", "blogg", "blog", "artikler", "articles", "media", "investor"),
    "locations": ("avdeling", "kontorer", "offices", "locations", "lokasjon", "butikk", "stores", "finn-butikk", "forhandler"),
}

_DATE_PATTERNS = [
    (re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})"), "ymd"),
    (re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b"), "dmy"),
    (re.compile(r"\b(\d{1,2})\.?\s+(januar|februar|mars|april|mai|juni|juli|august|september|oktober|november|desember|january|february|march|may|june|july|october|december)\s+(20\d{2})\b", re.I), "dMy"),
]
_MONTHS = {m: i for i, m in enumerate(["januar", "februar", "mars", "april", "mai", "juni", "juli", "august", "september", "oktober", "november", "desember"], 1)}
_MONTHS.update({"january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7, "october": 10, "december": 12})


def parse_date(text: str) -> str | None:
    for pattern, kind in _DATE_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        try:
            if kind == "ymd":
                y, m, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
            elif kind == "dmy":
                d, m, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
            else:
                d, m, y = int(match.group(1)), _MONTHS[match.group(2).lower()], int(match.group(3))
            return datetime(y, m, d).date().isoformat()
        except (ValueError, KeyError):
            continue
    return None


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


def normalize_social(platform: str, url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    host = parts.netloc.lower()
    if platform == "linkedin":
        host = "www.linkedin.com"
        segs = [s for s in path.split("/") if s][:2]
        path = "/" + "/".join(segs)
    elif platform in {"facebook", "instagram", "x", "tiktok", "youtube", "threads"}:
        host = {"facebook": "www.facebook.com", "instagram": "www.instagram.com", "x": "x.com", "tiktok": "www.tiktok.com",
                "youtube": "www.youtube.com", "threads": "www.threads.net"}[platform]
        segs = [s for s in path.split("/") if s]
        keep = 2 if platform == "youtube" and segs and segs[0] in {"channel", "c", "user"} else 1
        if platform == "facebook" and segs and segs[0] in {"pages", "pg"} and len(segs) >= 2:
            keep = min(len(segs), 3)
        path = "/" + "/".join(segs[:keep])
        if platform == "facebook" and segs and segs[0] == "profile.php":
            match = re.search(r"(?:^|&)id=(\d+)", parts.query)
            path = f"/profile.php?id={match.group(1)}" if match else path
    return f"https://{host}{path}"


def _jsonld_objects(soup: BeautifulSoup) -> list[dict]:
    objects = []
    for script in soup.find_all("script", type=lambda t: t and "ld+json" in t.lower()):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except ValueError:
                continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if "@graph" in item and isinstance(item["@graph"], list):
                    stack.extend(item["@graph"])
                objects.append(item)
            elif isinstance(item, list):
                stack.extend(item)
    return objects


def _types(obj: dict) -> set[str]:
    value = obj.get("@type")
    values = value if isinstance(value, list) else [value]
    return {str(v) for v in values if v}


ORG_TYPES = {"Organization", "Corporation", "LocalBusiness", "Store", "Restaurant", "ProfessionalService", "LegalService",
             "AutoDealer", "HomeAndConstructionBusiness", "MedicalBusiness", "FinancialService", "RealEstateAgent",
             "AccountingService", "Dentist", "Electrician", "Plumber", "GeneralContractor", "HVACBusiness", "LodgingBusiness",
             "Hotel", "FoodEstablishment", "HealthAndBeautyBusiness", "SportsActivityLocation", "TravelAgency", "EducationalOrganization",
             "NGO", "Physician", "AutoRepair", "RoofingContractor", "MovingCompany", "InsuranceAgency", "EmploymentAgency"}


def _text_value(value) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _text_value(value.get("name") or value.get("@value") or value.get("url"))
    if isinstance(value, list) and value:
        return _text_value(value[0])
    return None


def _address(value) -> str | None:
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        parts = [value.get("streetAddress"), " ".join(filter(None, [str(value.get("postalCode") or ""), str(value.get("addressLocality") or "")])).strip()]
        joined = ", ".join(p for p in parts if p)
        return joined or None
    return None


def extract_page(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    jsonld = _jsonld_objects(soup)
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    text = re.sub(r"[ \t\r\f\v]+", " ", soup.get_text("\n"))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()

    def meta(*names):
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    page = {
        "title": (soup.title.get_text(" ", strip=True) if soup.title else None) or None,
        "site_name": meta("og:site_name"),
        "description": meta("description", "og:description"),
        "og_title": meta("og:title"),
        "language": (soup.html.get("lang") if soup.html else None),
        "text": text[:60000],
        "org_numbers": sorted(find_org_numbers(text)),
        "social": {},
        "emails": set(),
        "phones": set(),
        "links": {k: [] for k in LINK_KINDS},
        "ats_links": [],
        "rss": [],
        "jsonld_orgs": [],
        "job_postings": [],
        "articles": [],
        "internal_links": [],
    }
    host = urlsplit(base_url).netloc.lower().removeprefix("www.")

    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        kind = (link.get("type") or "").lower()
        if "alternate" in rel and ("rss" in kind or "atom" in kind):
            page["rss"].append(urljoin(base_url, link["href"]))

    seen_internal = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        label = anchor.get_text(" ", strip=True)[:120]
        if href.startswith("mailto:"):
            email = href[7:].split("?")[0].strip().lower()
            if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
                page["emails"].add(email)
            continue
        if href.startswith("tel:"):
            digits = re.sub(r"[^\d+]", "", href[4:])
            if len(re.sub(r"\D", "", digits)) >= 8:
                page["phones"].add(digits)
            continue
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in {"http", "https"}:
            continue
        link_host = parts.netloc.lower().removeprefix("www.")
        for platform, rule in SOCIAL_RULES:
            match = rule.match(absolute)
            if match:
                tail = [s for s in parts.path.split("/") if s]
                if not tail or tail[-1].lower() in _SOCIAL_BAD_TAIL and len(tail) == 1:
                    break
                normalized = normalize_social(platform, absolute)
                if not normalized.endswith("/profile.php"):
                    page["social"].setdefault(platform, set()).add(normalized)
                break
        else:
            if any(ats in absolute.lower() for ats in ATS_HOSTS) and link_host != host:
                page["ats_links"].append({"url": clean_url(absolute), "label": label})
            if link_host == host or link_host.endswith("." + host):
                key = clean_url(absolute)
                if key in seen_internal:
                    continue
                seen_internal.add(key)
                page["internal_links"].append({"url": key, "label": label})
                path_l = (parts.path + " " + label).lower()
                for kind, words in LINK_KINDS.items():
                    if any(w in path_l for w in words):
                        page["links"][kind].append({"url": key, "label": label})

    for obj in jsonld:
        types = _types(obj)
        if types & ORG_TYPES or any(t.endswith("Business") or t.endswith("Organization") for t in types):
            same_as = obj.get("sameAs") or []
            same_as = same_as if isinstance(same_as, list) else [same_as]
            page["jsonld_orgs"].append({
                "type": sorted(types)[0] if types else None,
                "name": _text_value(obj.get("name")),
                "legal_name": _text_value(obj.get("legalName")),
                "url": _text_value(obj.get("url")),
                "description": _text_value(obj.get("description")),
                "telephone": _text_value(obj.get("telephone")),
                "email": _text_value(obj.get("email")),
                "address": _address(obj.get("address")),
                "founding_date": _text_value(obj.get("foundingDate")),
                "employees": _text_value(obj.get("numberOfEmployees")),
                "vat_id": _text_value(obj.get("vatID") or obj.get("taxID") or obj.get("identifier")),
                "logo": _text_value(obj.get("logo")),
                "same_as": [s for s in same_as if isinstance(s, str)],
            })
            for s in same_as:
                if isinstance(s, str):
                    for platform, rule in SOCIAL_RULES:
                        if rule.match(s):
                            normalized = normalize_social(platform, s)
                            if not normalized.endswith("/profile.php"):
                                page["social"].setdefault(platform, set()).add(normalized)
                            break
        if "JobPosting" in types:
            page["job_postings"].append({
                "title": _text_value(obj.get("title")),
                "date_posted": _text_value(obj.get("datePosted")),
                "valid_through": _text_value(obj.get("validThrough")),
                "location": _address(((obj.get("jobLocation") or {}) if isinstance(obj.get("jobLocation"), dict) else {}).get("address")) if obj.get("jobLocation") else None,
                "employment_type": _text_value(obj.get("employmentType")),
                "hiring_organization": _text_value(obj.get("hiringOrganization")),
                "url": _text_value(obj.get("url")) or base_url,
            })
        if types & {"NewsArticle", "BlogPosting", "Article", "PressRelease", "Report"}:
            published = _text_value(obj.get("datePublished")) or _text_value(obj.get("dateCreated"))
            if published:
                page["articles"].append({
                    "title": _text_value(obj.get("headline")) or _text_value(obj.get("name")),
                    "date": published[:10],
                    "url": _text_value(obj.get("url")) or base_url,
                })

    page["social"] = {k: sorted(v) for k, v in page["social"].items()}
    page["emails"] = sorted(page["emails"])
    page["phones"] = sorted(page["phones"])
    return page


# ------------------------------------------------------------------ feeds and sitemaps

def parse_feed(xml_text: str) -> list[dict]:
    soup = BeautifulSoup(xml_text, "html.parser")
    items = []
    for item in soup.find_all(["item", "entry"])[:50]:
        title = item.find("title")
        link = item.find("link")
        href = (link.get("href") or link.get_text(strip=True)) if link else None
        date_tag = item.find(["pubdate", "published", "dc:date"])  # a feed "updated" date is not a publish date
        date = None
        if date_tag:
            raw = date_tag.get_text(strip=True)
            for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M %z"):
                try:
                    date = datetime.strptime(raw, fmt).date().isoformat()
                    break
                except ValueError:
                    continue
            date = date or (raw[:10] if re.match(r"\d{4}-\d{2}-\d{2}", raw) else None)
        if title and href:
            items.append({"title": title.get_text(" ", strip=True)[:200], "url": href.strip(), "date": date})
    return items


def parse_sitemap(xml_text: str) -> tuple[list[str], list[dict]]:
    """Return (child sitemaps, url entries with optional lastmod)."""
    children = re.findall(r"<sitemap>\s*<loc>\s*([^<\s]+)\s*</loc>", xml_text, re.I)
    entries = []
    for block in re.findall(r"<url>(.*?)</url>", xml_text, re.I | re.S):
        loc = re.search(r"<loc>\s*([^<\s]+)\s*</loc>", block, re.I)
        lastmod = re.search(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", block, re.I)
        if loc:
            entries.append({"url": loc.group(1).replace("&amp;", "&"), "lastmod": lastmod.group(1)[:10] if lastmod else None})
    return children, entries


_JOB_PATH = re.compile(r"/(jobs?|stilling(er)?|ledige-stillinger|karriere|career|vacanc(y|ies)|positions?)/[^/?#]{6,}", re.I)
_NEWS_PATH = re.compile(r"/(nyheter|news|aktuelt|presse|pressemeldinger|blogg?|artikler|articles|insights|media)/[^/?#]{6,}", re.I)


def classify_url(url: str) -> str | None:
    if _JOB_PATH.search(url):
        return "job"
    if _NEWS_PATH.search(url):
        return "news"
    return None


def title_from_slug(url: str) -> str:
    slug = [s for s in urlsplit(url).path.split("/") if s][-1]
    slug = re.sub(r"\.(html?|php|aspx)$", "", slug)
    slug = re.sub(r"^\d+[-_]", "", slug)
    words = re.sub(r"[-_]+", " ", slug).strip()
    return words[:1].upper() + words[1:] if words else url
