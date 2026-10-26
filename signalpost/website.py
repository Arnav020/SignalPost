"""Website discovery, exact-identity gate and targeted static crawl of company-owned pages."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .extract import SOCIAL_RULES, classify_url, extract_page, normalize_social, org_context, parse_date, parse_feed, parse_sitemap
from .http import FetchResult, Http, is_safe_public_url
from .registry import FREE_MAIL, Registry, host_key

PLATFORM_HOSTS = ("facebook.com", "instagram.com", "linkedin.com", "youtube.com", "twitter.com", "x.com", "tiktok.com")
DIRECTORY_HOSTS = ("proff.no", "gulesider.no", "1881.no", "purehelp.no", "brreg.no", "finn.no", "google.", "bing.com",
                   "wix.com", "wixsite.com", "squarespace.com", "webnode", "blogspot.", "wordpress.com", "123hjemmeside",
                   "mittanbud.no", "anbud365", "enirofinans", "bedriftsdatabasen", "regnskapstall.no", "forvalt.no", "styrerommet")
PARKED_MARKERS = ("domain is for sale", "domenet er til salgs", "this domain may be for sale", "buy this domain", "parked free",
                  "domeneshop", "domain parking", "denne siden er under konstruksjon", "website coming soon",
                  "this site can’t be reached", "account suspended", "default web site page", "it works!", "index of /",
                  "parkert", "domene er registrert", "under construction", "kommer snart", "coming soon")
LEGAL_SUFFIXES = {"as", "asa", "ans", "da", "sa", "ba", "enk", "nuf", "ks", "stiftelsen", "stiftelse", "holding", "holdings",
                  "invest", "eiendom", "group", "gruppen", "norge", "norway", "norsk", "og", "&", "i", "the", "of", "avd",
                  "avdeling", "co", "company", "selskap", "selskapet", "drift", "service", "services", "konsern", "sameiet",
                  "borettslag", "brl", "eiendommer", "invest", "capital", "partners", "management"}


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = text.replace("ø", "o").replace("æ", "ae").replace("å", "a")
    return "".join(c for c in text if not unicodedata.combining(c))


def name_tokens(name: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", fold(name))
    return [t for t in tokens if t not in LEGAL_SUFFIXES and len(t) >= 2]


@dataclass
class Candidate:
    url: str
    host: str
    origin: str  # registry_website | registry_email | subunit_website | subunit_email
    exclusive: bool  # domain registered to this organisation only
    owners: int


@dataclass
class SiteResult:
    candidate: Candidate
    identity: str = "not_checked"  # exact | strong | registry_declared | ambiguous | conflict | unreachable | blocked | parked
    proofs: list[dict] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)
    extracted: dict = field(default_factory=dict)
    home_url: str | None = None
    notes: list[str] = field(default_factory=list)
    scope: str = "full"  # full = company's own site; url_only = parent/group or unconfirmed site (publish the URL only)
    scope_reasons: list[str] = field(default_factory=list)


def social_from_registry_url(url: str) -> tuple[str, str] | None:
    candidate = url if url.startswith("http") else "https://" + url.strip()
    for platform, rule in SOCIAL_RULES:
        if rule.match(candidate):
            return platform, normalize_social(platform, candidate)
    return None


def build_candidates(org: str, entity: dict, subunits: list[dict], registry: Registry) -> tuple[list[Candidate], list[dict]]:
    """Candidates from official registry contact data only. Also returns social profiles declared as 'website'."""
    out: dict[str, Candidate] = {}
    declared_social = []

    def add(raw: str, origin: str):
        if not raw:
            return
        if origin.endswith("website"):
            social = social_from_registry_url(raw)
            if social:
                declared_social.append({"platform": social[0], "url": social[1], "origin": origin, "raw": raw})
                return
        host = host_key(raw)
        if not host or host in FREE_MAIL or any(host == d or host.endswith("." + d) or d in host for d in PLATFORM_HOSTS + DIRECTORY_HOSTS):
            return
        owners = registry.owners(host)
        exclusive = owners <= {org}
        if host in out:
            return
        if origin.endswith("website"):
            raw_url = raw.strip()
            url = raw_url if raw_url.startswith("http") else "https://" + raw_url.lstrip("/")
        else:
            url = f"https://{host}/"
        out[host] = Candidate(url=url, host=host, origin=origin, exclusive=exclusive, owners=max(1, len(owners)))

    add(entity.get("website", ""), "registry_website")
    add(entity.get("email", ""), "registry_email")
    for sub in subunits or []:
        add(sub.get("website", ""), "subunit_website")
    for sub in subunits or []:
        add(sub.get("email", ""), "subunit_email")
    order = {"registry_website": 0, "registry_email": 1, "subunit_website": 2, "subunit_email": 3}
    candidates = sorted(out.values(), key=lambda c: (not c.exclusive and not c.origin.endswith("website"), order[c.origin]))
    return candidates, declared_social


FOREIGN_TLDS = {"se", "dk", "fi", "de", "uk", "nl", "fr", "pl", "ee", "lt", "lv", "is", "es", "it", "be", "at", "ch", "us", "ru", "cn"}
GROUPISH = re.compile(r"\b(group|gruppen|groupe|konsern|konsernet|holding|holdings)\b")
GROUP_LISTING = re.compile(r"vare selskaper|vare datterselskaper|selskapene i konsernet|selskaper i konsernet|our companies|"
                           r"our subsidiaries|group companies|companies in the group|vare virksomheter|selskapene vare")
GENERIC_BRANDS = {"my site", "home", "hjem", "forside", "velkommen", "welcome", "startside", "index", "untitled", "default",
                  "just another wordpress site", "my wordpress site", "site title", "new site", "hjemmeside"}
THEME_HANDLES = {"wix", "wordpress", "wordpressdotcom", "envato", "themeforest", "elementor", "squarespace", "godaddy", "onecom",
                 "domeneshop", "webflow", "shopify", "mailchimp", "hubspot", "jimdo", "weebly", "joomla", "drupal", "facebook",
                 "instagram", "linkedin", "youtube", "twitter", "sharer", "plugins", "themeisle", "astra", "divi", "elegantthemes",
                 "visma", "tripletex", "fiken", "uni", "proff", "gulesider", "1881"}
NAV_SLUGS = {"om-oss", "omoss", "about", "about-us", "kontakt", "kontakt-oss", "contact", "contact-us", "nyheter", "news", "aktuelt",
             "blogg", "blog", "presse", "press", "artikler", "articles", "arkiv", "archive", "kategori", "category", "tag", "side",
             "page", "produkter", "products", "tjenester", "services", "shop", "butikk", "nettbutikk", "ansatte", "karriere",
             "jobb", "jobs", "career", "careers", "personvern", "privacy", "cookies", "vilkar", "terms", "ukategorisert", "uncategorized",
             "referanser", "prosjekter", "projects", "galleri", "gallery", "faq", "priser", "prices", "medlem", "login", "logg-inn"}
NON_ARTICLE_PATH = re.compile(r"/(produkt|produkter|product|products|shop|butikk|nettbutikk|kategori|categories|category|tag|tags|merke|"
                              r"brand|vare|varer|collections|collection|cart|handlekurv|kasse|checkout|author|forfatter|page|side)/", re.I)
PRICE_TEXT = re.compile(r"\b(kr|nok|pris|kjop|kjøp|inkl\.?\s*mva|eks\.?\s*mva|legg i handlekurv|add to cart|på lager)\b", re.I)


def article_like(url: str, home_url: str | None, index_url: str | None = None) -> bool:
    """A real post has its own URL: not the home page, the news index, a menu page, a product or a category page."""
    parts = urlsplit(url or "")
    path = parts.path.rstrip("/")
    if not path or (url or "").rstrip("/") in {(home_url or "").rstrip("/"), (index_url or "").rstrip("/")}:
        return False
    if NON_ARTICLE_PATH.search(path + "/"):
        return False
    slug = re.sub(r"\.(html?|php|aspx?)$", "", path.rsplit("/", 1)[-1].lower())
    if slug in NAV_SLUGS or len(slug) < 8:
        return False
    return len(re.split(r"[-_]+", slug)) >= 3 or bool(re.search(r"/20\d\d/|\d{4,}", path))


def _domain_is_name_part(host: str, tokens: list[str], entity: dict) -> bool:
    """Domain = a distinctive part of the legal name (e.g. evotec.no for ENTEC EVOTEC AS, saltofilm.no for SALTO FILM
    OG FJERNSYN AS). Place names (the company's own municipality/city) are not distinctive: drammen.no does not count."""
    label = re.sub(r"[^a-z0-9]", "", fold(host.split(".")[0]))
    variants = {label, label.replace("ae", "a")}
    places = {re.sub(r"[^a-z0-9]", "", fold(entity.get(k) or "")) for k in ("municipality", "business_city", "postal_city")}
    toks = [re.sub(r"[^a-z0-9]", "", t) for t in tokens]
    toks_a = [t.replace("ae", "a") for t in toks]
    for seq in (toks, toks_a):
        for t in seq:
            if len(t) >= 5 and t not in places and t in variants:
                return True
        for k in range(2, len(seq) + 1):  # leading tokens joined: "salto"+"film"
            joined = "".join(seq[:k])
            if len(joined) >= 6 and joined in variants:
                return True
    return False


def _today() -> str:
    from datetime import date as _date

    return _date.today().isoformat()


def _months_ago(months: int) -> str:
    from datetime import date as _date, timedelta

    return (_date.today() - timedelta(days=31 * months)).isoformat()


def real_title(title: str | None) -> bool:
    title = (title or "").strip()
    return 15 <= len(title) <= 220 and not PRICE_TEXT.search(fold(title)) and fold(title) not in NAV_SLUGS


def valid_publish_date(value: str | None) -> bool:
    if not value or not re.match(r"\d{4}-\d{2}-\d{2}", value):
        return False
    from datetime import date as _date

    return "2000-01-01" <= value[:10] <= _date.today().isoformat()


NO_GUESS_FORMS ={"BRL", "BBL", "ESEK", "SAM", "KIRK", "PK", "VPFO", "KF", "IKS"}
NO_GUESS_WORDS = {"borettslag", "sameie", "sameiet", "boligsameie", "holding", "holdings", "invest", "investering", "eiendom",
                  "eiendommer", "eiendomsselskap", "konsult", "konsulent", "kapital", "capital", "partner", "partners"}
GENERIC_SINGLE = {"bygg", "transport", "service", "data", "consult", "consulting", "media", "design", "eiendom", "invest", "norge",
                  "norway", "nordic", "group", "elektro", "maskin", "auto", "bil", "regnskap", "advokat", "tannlege", "frisor",
                  "handel", "solutions", "tech", "energy", "marine", "shipping", "drift", "utleie", "montasje", "renhold", "maling"}


def guess_candidates(org: str, entity: dict, registry: Registry, max_guesses: int = 3) -> list[Candidate]:
    """Domain guesses from the legal name. Only used when the registry offers no candidate; publication needs hard proof."""
    if (entity.get("legal_form") or "").upper() in NO_GUESS_FORMS:
        return []
    raw_tokens = re.findall(r"[a-z0-9]+", fold(entity.get("name", "")))
    if any(t in NO_GUESS_WORDS for t in raw_tokens):
        return []
    tokens = [t for t in raw_tokens if t not in {"as", "asa", "ans", "da", "sa", "ba", "enk", "nuf", "ks", "og", "the", "avd"}]
    if not tokens or len(tokens) > 4:
        return []
    joined = "".join(tokens)
    if len(joined) < 5 or (len(tokens) == 1 and tokens[0] in GENERIC_SINGLE) or joined.isdigit():
        return []
    names = [joined]
    if len(tokens) > 1:
        names.append("-".join(tokens))
    names.append(joined + "as")
    out = []
    for name in names:
        for tld in ("no", "com"):
            host = f"{name}.{tld}"
            if len(out) >= max_guesses + 3:
                break
            owners = registry.owners(host)
            if owners and owners != {org}:
                continue  # registered to another organisation
            out.append(Candidate(url=f"https://{host}/", host=host, origin="name_guess", exclusive=True, owners=1))
    return out


def _is_html(res: FetchResult) -> bool:
    ctype = res.content_type.lower()
    return ("html" in ctype or not ctype) and bool(res.body)


def _parked(page: dict, res: FetchResult) -> bool:
    text = fold(page.get("text", ""))[:4000]
    title = fold(page.get("title") or "")
    if len(text) < 80 and not page["internal_links"]:
        return True
    return any(marker in text[:1500] or marker in title for marker in PARKED_MARKERS) and len(page["internal_links"]) < 8


class SiteCrawler:
    def __init__(self, http: Http):
        self.http = http

    def _fetch_page(self, url: str, site: SiteResult, kind: str, *, check_robots: bool = True) -> tuple[FetchResult | None, dict | None]:
        if check_robots and not self.http.allowed(url):
            site.notes.append(f"robots_disallowed:{url}")
            return None, None
        res = self.http.get(url, "company_website", timeout=12, polite_delay=0.5)
        record = {"kind": kind, "requested_url": url, "final_url": res.final_url or url, "status": res.status,
                  "retrieved_at": res.retrieved_at, "sha256": res.sha256, "redirects": res.redirects,
                  "error": res.error, "elapsed_ms": res.elapsed_ms}
        site.pages.append(record)
        if not res.ok or not _is_html(res):
            return res, None
        page = extract_page(res.text(), res.final_url or url)
        record["title"] = page.get("title")
        return res, page

    def crawl(self, org: str, entity: dict, roles: list[dict], candidate: Candidate, page_budget: int) -> SiteResult:
        site = SiteResult(candidate=candidate)
        attempts = [candidate.url]
        if candidate.origin.endswith("email") or not candidate.url.startswith("https://www."):
            alt = f"https://www.{candidate.host}/"
            if alt not in attempts:
                attempts.append(alt)
        if candidate.url.startswith("https://"):
            attempts.append("http://" + candidate.url[8:])
        home = None
        res = None
        used = 0
        paid_attempts = 0
        for url in attempts:
            if used >= page_budget or paid_attempts >= 2:
                break
            if self.http.budget.remaining() <= 0:
                site.identity = "blocked"
                site.notes.append("budget_exhausted")
                return site
            if not is_safe_public_url(url):
                continue  # DNS does not resolve: free to skip, try the next variant
            allowed = self.http.allowed(url)
            if (urlsplit(url).hostname or "").lower().removeprefix("www.") in self.http.dead_hosts:
                paid_attempts += 1
                continue  # robots.txt fetch could not connect: do not spend more requests on this variant
            if not allowed:
                site.identity = "blocked"
                site.notes.append("robots_disallowed_home")
                return site
            used += 1
            paid_attempts += 1
            res, home = self._fetch_page(url, site, "home", check_robots=False)
            if home is not None:
                break
            if res is not None and res.status in (401, 403, 429):
                site.identity = "blocked"
                return site
        if home is None:
            # No variant resolved in DNS: the domain does not exist (not available). Otherwise it timed out/errored (failed).
            site.identity = "dead_domain" if paid_attempts == 0 else "unreachable"
            return site
        site.home_url = res.final_url or candidate.url
        final_host = host_key(site.home_url)
        if final_host and final_host != candidate.host and any(final_host.endswith(d) for d in PLATFORM_HOSTS + DIRECTORY_HOSTS):
            site.identity = "conflict"
            site.notes.append(f"redirects_to_platform:{final_host}")
            return site
        if _parked(home, res):
            site.identity = "parked"
            return site

        pages = [(site.home_url, home)]
        found_ours = org in home["org_numbers"]
        # Pick follow-up pages in value order.
        plan: list[tuple[str, str]] = []
        if not found_ours:
            for kind in ("contact", "about"):
                if home["links"][kind]:
                    plan.append((kind, home["links"][kind][0]["url"]))
                    break
        if home["links"]["careers"]:
            plan.append(("careers", home["links"]["careers"][0]["url"]))
        if home["links"]["news"]:
            plan.append(("news", home["links"]["news"][0]["url"]))
        if home["links"]["people"]:
            plan.append(("people", home["links"]["people"][0]["url"]))
        if found_ours and home["links"]["about"]:
            plan.append(("about", home["links"]["about"][0]["url"]))
        if home["links"]["locations"]:
            plan.append(("locations", home["links"]["locations"][0]["url"]))
        seen = {site.home_url.rstrip("/")}
        for kind, url in plan:
            if used >= page_budget or self.http.budget.past_soft_deadline():
                break
            if url.rstrip("/") in seen:
                continue
            seen.add(url.rstrip("/"))
            used += 1
            _, page = self._fetch_page(url, site, kind)
            if page is not None:
                pages.append((url, page))
        # Feeds give dated activity cheaply.
        feed_items = []
        # Feeds and sitemaps have low yield per request: only when the run budget is comfortable.
        plenty = self.http.budget.remaining() > 0.35 * self.http.budget.max_requests
        rss = next((p["rss"][0] for _, p in pages if p["rss"]), None)
        if rss and plenty and used < page_budget and not self.http.budget.past_soft_deadline():
            used += 1
            if self.http.allowed(rss):
                fres = self.http.get(rss, "company_website_feed", timeout=10, polite_delay=0.5)
                site.pages.append({"kind": "feed", "requested_url": rss, "final_url": fres.final_url or rss, "status": fres.status,
                                   "retrieved_at": fres.retrieved_at, "sha256": fres.sha256, "error": fres.error})
                if fres.ok:
                    feed_items = [dict(item, source_url=rss, retrieved_at=fres.retrieved_at, sha256=fres.sha256) for item in parse_feed(fres.text())]
        # Sitemap lastmod dates are not publication dates, so sitemaps are no longer used for news or jobs.
        sitemap_items: list[dict] = []
        self._evaluate_identity(org, entity, roles, site, pages)
        site.extracted = self._aggregate(org, entity, roles, site, pages, feed_items, sitemap_items)
        return site

    def _sitemap(self, site: SiteResult, budget: int) -> list[dict]:
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(site.home_url))
        parser = self.http._robots.get((urlsplit(site.home_url).hostname or "").lower().removeprefix("www."))
        maps = list(parser.site_maps() or []) if parser else []
        url = maps[0] if maps else origin + "/sitemap.xml"
        items: list[dict] = []
        for _ in range(min(2, budget)):
            if not self.http.allowed(url):
                break
            res = self.http.get(url, "company_website_sitemap", timeout=10, polite_delay=0.5, max_bytes=4_000_000)
            site.pages.append({"kind": "sitemap", "requested_url": url, "final_url": res.final_url or url, "status": res.status,
                               "retrieved_at": res.retrieved_at, "sha256": res.sha256, "error": res.error})
            if not res.ok:
                break
            children, entries = parse_sitemap(res.text())
            for entry in entries:
                kind = classify_url(entry["url"])
                if kind and host_key(entry["url"]) == host_key(site.home_url):
                    items.append(dict(entry, kind=kind, source_url=res.final_url or url, retrieved_at=res.retrieved_at, sha256=res.sha256))
            if entries or not children:
                break
            preferred = [c for c in children if re.search(r"post|news|nyhet|artikkel|article|job|stilling", c, re.I)]
            url = (preferred or children)[0]
        return items

    # ------------------------------------------------------------------ identity gate
    def _evaluate_identity(self, org: str, entity: dict, roles: list[dict], site: SiteResult, pages: list[tuple[str, dict]]) -> None:
        cand = site.candidate
        all_orgs: set[str] = set()
        for url, page in pages:
            all_orgs.update(page["org_numbers"])
            if org in page["org_numbers"]:
                span = org_context(page["text"], org)
                site.proofs.append({"type": "organisation_number_on_site", "url": url, "span": span})
        others = all_orgs - {org}
        head_text = fold(" ".join(filter(None, [pages[0][1].get("title"), pages[0][1].get("site_name"), pages[0][1].get("og_title")]
                                       + [o.get("name") or "" for o in pages[0][1]["jsonld_orgs"]] + [o.get("legal_name") or "" for o in pages[0][1]["jsonld_orgs"]])))
        body_text = fold(" ".join(p["text"][:20000] for _, p in pages))
        tokens = name_tokens(entity.get("name", ""))
        # The legal name must appear as a phrase (tokens in order, adjacent), not as scattered common words.
        phrase = r"\b" + r"[\W_]{0,3}".join(re.escape(t) for t in tokens) + r"\b" if tokens else None
        name_match = bool(phrase) and bool(re.search(phrase, head_text + " \n " + body_text))
        name_in_head = bool(phrase) and bool(re.search(phrase, head_text))
        domain_compact = re.sub(r"[^a-z0-9]", "", cand.host.split(".")[0])
        name_compact = "".join(tokens)
        domain_name = bool(name_compact) and len(name_compact) >= 4 and (
            name_compact in domain_compact or (domain_compact in name_compact and len(domain_compact) >= max(5, 0.6 * len(name_compact))))
        if name_match:
            site.proofs.append({"type": "legal_name_tokens_on_site", "tokens": tokens, "in_title": name_in_head})
        if domain_name:
            site.proofs.append({"type": "domain_matches_legal_name", "domain": cand.host})
        page_digits = re.sub(r"\D", "", " ".join(p["text"] for _, p in pages)) + " " + " ".join(re.sub(r"\D", "", ph) for _, p in pages for ph in p["phones"])
        phone_match = False
        for raw in (entity.get("phone"), entity.get("mobile")):
            digits = re.sub(r"\D", "", raw or "")[-8:]
            if len(digits) == 8 and digits in page_digits:
                phone_match = True
                site.proofs.append({"type": "registry_phone_on_site", "phone": digits})
                break
        address_match = False
        postcode = (entity.get("business_postcode") or "").strip()
        street = fold((entity.get("business_street") or "").split("\n")[0])
        street_word = re.findall(r"[a-z]{4,}", street)
        if postcode and re.search(rf"\b{postcode}\b", body_text) and (not street_word or street_word[0] in body_text):
            address_match = True
            site.proofs.append({"type": "registry_address_on_site", "postcode": postcode})
        person_hits = []
        for role in roles:
            name = fold(role.get("name") or "")
            if role.get("person") and len(name.split()) >= 2 and name in body_text:
                person_hits.append(role["name"])
        if person_hits:
            site.proofs.append({"type": "registered_role_holder_on_site", "names": sorted(set(person_hits))[:5]})
        corroborations = sum([phone_match, address_match, bool(person_hits)])

        named = name_match or domain_name
        if not cand.exclusive and org in all_orgs and named and not others:
            site.identity = "exact"  # shared contact domain, but the site itself names and numbers this organisation
        elif not cand.exclusive:
            # A domain registered for several organisations (manager, accountant, group, franchise) is never the
            # company's own site, even when our organisation number is listed on it.
            site.identity = "shared_site" if (org in all_orgs or cand.origin == "registry_website") else "ambiguous"
            site.notes.append(f"domain_shared_by_{cand.owners}_registry_organisations")
        elif org in all_orgs and (len(others) == 0 or named):
            site.identity = "exact"
        elif org in all_orgs:
            site.identity = "shared_site"
            site.notes.append(f"several_organisation_numbers_on_site:{sorted(others)[:5]}")
        elif others:
            site.identity = "conflict"
            site.notes.append(f"other_organisation_numbers_on_site:{sorted(others)[:5]}")
        elif cand.origin == "name_guess":
            # Guessed domains need hard proof: our number (handled above) or brand-in-title + domain + contact match.
            site.identity = "strong" if (domain_name and name_match and corroborations) else "ambiguous"
        elif cand.origin in ("registry_website", "nav_employer_homepage", "wikidata_website"):
            # Declared by the company itself in an official source tied to its organisation number.
            site.identity = "strong" if named and (corroborations or name_in_head or domain_name) else "registry_declared"
        elif named and corroborations or (name_in_head and domain_name) or corroborations >= 2 and named:
            site.identity = "strong"
        else:
            site.identity = "ambiguous"
        site.notes.append(f"name_match={name_match};domain_name={domain_name};corroborations={corroborations};others={len(others)}")

        # Content scope: is this the company's OWN site, or a parent/group site that merely lists it?
        # On a group site only the URL is published; its profiles, news and jobs are not attributed to this company.
        reasons = []
        legal = fold(entity.get("name", ""))
        final_host = host_key(site.home_url or cand.url)
        if len(others) >= 2:
            reasons.append("several_other_organisation_numbers_on_site")
        if final_host.rsplit(".", 1)[-1] in FOREIGN_TLDS and org not in all_orgs:
            reasons.append("foreign_domain_without_this_organisation_number")
        if GROUPISH.search(head_text) and not GROUPISH.search(legal):
            reasons.append("group_branded_site")
        if GROUP_LISTING.search(body_text[:30000]) and org not in all_orgs:
            reasons.append("site_lists_group_companies")
        if org not in all_orgs and not name_in_head and not domain_name and not _domain_is_name_part(cand.host, tokens, entity):
            reasons.append("company_name_not_in_site_title_or_domain")
        site.scope = "url_only" if reasons else "full"
        site.scope_reasons = reasons

    @staticmethod
    def _own_profiles(entity: dict, site: SiteResult, social: dict, pages, home: dict) -> dict:
        """Keep only the company's own profiles: partner/customer/press links on a page are not its profiles.

        Per platform: a single profile linked site-wide is accepted; with several, only handles matching the
        legal name, brand or domain survive.
        """
        keys = set(name_tokens(entity.get("name", "")))
        for text in (home.get("site_name"), home.get("og_title")):
            if fold(text or "").strip() not in GENERIC_BRANDS:
                keys.update(t for t in name_tokens(text or "") if len(t) >= 3)
        keys -= THEME_HANDLES
        label = re.sub(r"[^a-z0-9]", "", fold(site.candidate.host.split(".")[0]))
        compact_name = "".join(name_tokens(entity.get("name", "")))

        def matches(url: str) -> bool:
            # Every kept profile must carry the company's own name/brand/domain in its handle: footer links to a parent
            # group, the web agency or the site theme do not, and numeric IDs cannot be verified.
            handle = re.sub(r"[^a-z0-9]", "", fold(url.split("?")[0].rstrip("/").rsplit("/", 1)[-1].lstrip("@")))
            if not handle or handle.isdigit() or handle in THEME_HANDLES or any(t in handle for t in ("theme", "template", "webdesign", "webbyra", "digitalbyra")):
                return False
            if label and len(label) >= 4 and (label in handle or handle in label and len(handle) >= 4):
                return True
            if compact_name and len(compact_name) >= 4 and (compact_name in handle or handle in compact_name and len(handle) >= 4):
                return True
            return any(len(k) >= 4 and k in handle for k in keys)

        legal_tokens = [t for t in name_tokens(entity.get("name", "")) if len(t) >= 3]

        def token_score(url: str) -> int:
            handle = re.sub(r"[^a-z0-9]", "", fold(url.rstrip("/").rsplit("/", 1)[-1]))
            return sum(1 for t in legal_tokens if t in handle)

        by_platform: dict[str, list[dict]] = {}
        for item in social.values():
            by_platform.setdefault(item["platform"], []).append(item)
        kept = {}
        for platform, items in by_platform.items():
            good = [i for i in items if matches(i["url"])]
            if len(good) > 1:
                # Group/brand sites link sister companies: keep only a unique best match on the legal name.
                scores = sorted(((token_score(i["url"]), i) for i in good), key=lambda s: -s[0])
                best = scores[0][0]
                top = [i for s, i in scores if s == best]
                good = top if len(top) == 1 and best > 0 else []
            for i in good[:1]:
                kept[i["url"]] = i
        return kept

    # ------------------------------------------------------------------ aggregation
    def _aggregate(self, org, entity, roles, site: SiteResult, pages, feed_items, sitemap_items) -> dict:
        home = pages[0][1]
        social: dict[str, dict] = {}
        emails, phones = {}, {}
        jsonld_orgs, jobs, news, ats = [], [], [], []
        careers_url = None
        news_page = None
        for url, page in pages:
            for platform, urls in page["social"].items():
                for value in urls:
                    social.setdefault(value, {"platform": platform, "url": value, "source_url": url})
            for e in page["emails"]:
                emails.setdefault(e, url)
            for p in page["phones"]:
                phones.setdefault(p, url)
            jsonld_orgs.extend(dict(o, source_url=url) for o in page["jsonld_orgs"])
            for job in page["job_postings"]:
                # Structured JobPosting only, with a title and a current posting/validity date.
                fresh = valid_publish_date((job.get("date_posted") or "")[:10]) and (job.get("date_posted") or "")[:10] >= _months_ago(12)
                open_ = (job.get("valid_through") or "")[:10] >= _today()
                if job.get("title") and (fresh or open_):
                    jobs.append(dict(job, source_url=url, method="jsonld_jobposting"))
            for art in page["articles"]:
                news.append(dict(art, source_url=url, method="jsonld_article"))
            for link in page["ats_links"]:
                ats.append(dict(link, source_url=url))
        for url, page in pages[1:]:
            record = next((p for p in site.pages if p["requested_url"] == url), None)
            kind = record["kind"] if record else None
            if kind == "careers":
                careers_url = url  # the careers page itself; link labels on it are not treated as job postings
            if kind == "news":
                news_page = {"url": url, "title": page.get("title")}
                for link in page["internal_links"]:
                    # Only cards that carry their own publish date in the link text; menu labels have none.
                    date = parse_date(link["label"])
                    if date and classify_url(link["url"]) == "news":
                        title = re.sub(r"\s*\b\d{1,2}\.\s*\w+\s+20\d\d\b|\b\d{1,2}\.\d{1,2}\.20\d\d\b|\b20\d\d-\d{2}-\d{2}\b", " ", link["label"]).strip(" -|·,")
                        news.append({"title": title, "url": link["url"], "date": date, "source_url": url, "method": "news_page_dated_link"})
        for item in feed_items:
            news.append({"title": item["title"], "url": item["url"], "date": item.get("date"), "source_url": item["source_url"], "method": "rss_feed"})
        # Keep only real, dated, company-owned posts with their own article URL.
        own = host_key(site.home_url)
        index_url = (news_page or {}).get("url")
        news = [n for n in news
                if valid_publish_date(n.get("date")) and real_title(n.get("title")) and article_like(n.get("url"), site.home_url, index_url)
                and host_key(n.get("url") or "") == own]
        # Dedupe and keep the most recent first.
        def dedupe(items):
            out, seen = [], set()
            for item in items:
                key = (item.get("url") or "").rstrip("/") or item.get("title")
                if key in seen:
                    continue
                seen.add(key)
                out.append(item)
            return out

        social = self._own_profiles(entity, site, social, pages, home)
        news = sorted(dedupe(news), key=lambda n: n.get("date") or "", reverse=True)[:25]
        jobs = dedupe(jobs)[:25]
        ats = dedupe(ats)[:5]
        own_host = host_key(site.home_url)
        own_emails = {e: u for e, u in emails.items() if host_key(e) == own_host or host_key(e) == site.candidate.host}
        leaders_on_site = []
        body = fold(" ".join(p["text"] for _, p in pages))
        for role in roles:
            name = role.get("name") or ""
            if role.get("person") and len(name.split()) >= 2 and fold(name) in body:
                leaders_on_site.append({"name": name, "role": role.get("role")})
        description = home.get("description") or next((o.get("description") for o in jsonld_orgs if o.get("description")), None)
        brand = home.get("site_name") or next((o.get("name") for o in jsonld_orgs if o.get("name")), None)
        if brand and fold(brand).strip() in GENERIC_BRANDS:
            brand = None  # site-builder default such as "My Site"
        return {
            "home_url": site.home_url,
            "title": home.get("title"),
            "brand_name": brand,
            "description": description,
            "language": home.get("language"),
            "social_profiles": list(social.values()),
            "emails": [{"value": e, "source_url": u} for e, u in own_emails.items()][:5],
            "phones": [{"value": p, "source_url": u} for p, u in phones.items()][:5],
            "jsonld_organizations": jsonld_orgs[:3],
            "job_postings": jobs,
            "careers_page": careers_url,
            "ats_links": ats,
            "news": news,
            "news_page": news_page,
            "pages_captured": sum(1 for p in site.pages if p.get("status") == 200),
            "leaders_on_site": leaders_on_site[:10],
            "org_numbers_seen": sorted({o for _, p in pages for o in p["org_numbers"]})[:10],
        }
