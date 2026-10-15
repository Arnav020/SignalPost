"""Brønnøysund registry layer.

The whole batch is anchored with three official bulk files (one request each) instead of several
per-company API calls. This keeps nearly the entire request budget free for external discovery.
Per-company live API calls are only a fallback when a bulk file is unavailable.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .http import FetchResult, Http, utc_now

csv.field_size_limit(10_000_000)

BULK = {
    "entities": ("https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv", "enheter.csv.gz"),
    "subunits": ("https://data.brreg.no/enhetsregisteret/api/underenheter/lastned/csv", "underenheter.csv.gz"),
    "roles": ("https://data.brreg.no/enhetsregisteret/api/roller/totalbestand", "roller.json.gz"),
}
ENTITY_API = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
ROLES_API = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller"
SUBUNITS_API = "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=200"
ENTITY_PAGE = "https://virksomhet.brreg.no/nb/oppslag/enheter/{org}"

# Free-mail and ISP domains never identify a company website.
FREE_MAIL = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.no", "live.com",
    "msn.com", "yahoo.com", "yahoo.no", "icloud.com", "me.com", "mac.com", "online.no", "frisurf.no", "broadpark.no",
    "getmail.no", "c2i.net", "start.no", "lyse.net", "altibox.no", "mail.com", "gmx.com", "gmx.net", "protonmail.com",
    "proton.me", "telia.com", "telia.no", "combitel.no", "haugnett.no", "enivest.net", "tele2.no", "chello.no",
    "netcom.no", "bluezone.no", "loqal.no", "ebnett.no", "tdcnet.no", "vikenfiber.no", "nktv.no", "aol.com",
    "kvinnherad.net", "sfjbb.net", "tussa.com", "nettfiber.no", "signalbredbaand.no", "hesbynett.no", "mimer.no",
    "eidsiva.net", "online.com", "post.com", "email.com", "inbox.com", "ymail.com", "rocketmail.com", "live.se",
    "hotmail.se", "hotmail.dk", "yahoo.co.uk", "hotmail.co.uk", "sensewave.com", "tele.no", "sf-nett.no",
}


def host_key(value: str | None) -> str:
    """Normalise a website or email to a comparable host (lower-case, no www., no port)."""
    if not value:
        return ""
    value = value.strip().lower()
    if "@" in value:
        value = value.rsplit("@", 1)[-1]
    value = re.sub(r"^[a-z]+://", "", value)
    value = value.split("/")[0].split("?")[0].split(":")[0].strip(". ")
    if value.startswith("www."):
        value = value[4:]
    if not re.fullmatch(r"[a-z0-9æøåäöü.-]+\.[a-zæøå]{2,}", value):
        return ""
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Registry:
    """Holds the registry records for the batch plus global domain-ownership counts."""

    def __init__(self):
        self.entities: dict[str, dict] = {}
        self.subunits: dict[str, list[dict]] = {}
        self.roles: dict[str, dict] = {}
        self.domain_owners: dict[str, set[str]] = {}
        self.sources: dict[str, dict] = {}
        self.errors: list[str] = []

    def owners(self, domain: str) -> set[str]:
        return self.domain_owners.get(domain, set())

    def _own(self, domain: str, owner: str) -> None:
        if not domain or domain in FREE_MAIL:
            return
        owners = self.domain_owners.setdefault(domain, set())
        if len(owners) < 6:
            owners.add(owner)


def _open_text(path: Path):
    raw = path.open("rb")
    magic = raw.read(2)
    raw.seek(0)
    if magic == b"\x1f\x8b":
        return io.TextIOWrapper(gzip.GzipFile(fileobj=raw), encoding="utf-8", errors="replace", newline="")
    return io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")


def _fresh(path: Path, max_age_hours: float) -> bool:
    return path.exists() and path.stat().st_size > 1_000_000 and (time.time() - path.stat().st_mtime) < max_age_hours * 3600


def ensure_bulk(http: Http, cache_dir: Path, kind: str, max_age_hours: float, provided_dir: Path | None) -> tuple[Path | None, dict]:
    url, filename = BULK[kind]
    if provided_dir and provided_dir.is_file():
        provided_dir = provided_dir if kind == "entities" else None  # a single file is the entity snapshot (starter-kit --bulk)
        if provided_dir:
            return provided_dir, {"url": url, "path": str(provided_dir), "origin": "evaluator_supplied_snapshot",
                                  "retrieved_at": datetime.fromtimestamp(provided_dir.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    if provided_dir:
        for candidate in sorted(provided_dir.glob("*")):
            name = candidate.name.lower()
            if (kind == "entities" and "enhet" in name and "underenhet" not in name) or (kind == "subunits" and "underenhet" in name) or (kind == "roles" and "roll" in name):
                return candidate, {"url": url, "path": str(candidate), "origin": "evaluator_supplied_snapshot",
                                   "retrieved_at": datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    target = cache_dir / filename
    if _fresh(target, max_age_hours):
        return target, {"url": url, "path": str(target), "origin": "run_cache",
                        "retrieved_at": datetime.fromtimestamp(target.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    result: FetchResult = http.download(url, target, "official_registry_bulk")
    if result.error or not target.exists():
        return (target if target.exists() else None), {"url": url, "error": result.error, "origin": "download_failed"}
    return target, {"url": url, "path": str(target), "origin": "downloaded", "retrieved_at": result.retrieved_at}


ENTITY_COLUMNS = {
    "organisasjonsnummer": "organisation_number", "navn": "name", "organisasjonsform.kode": "legal_form",
    "organisasjonsform.beskrivelse": "legal_form_label", "naeringskode1.kode": "industry_code",
    "naeringskode1.beskrivelse": "industry_label", "naeringskode2.kode": "industry2_code",
    "naeringskode2.beskrivelse": "industry2_label", "antallAnsatte": "employees",
    "harRegistrertAntallAnsatte": "has_registered_employees",
    "registreringsdatoAntallAnsatteEnhetsregisteret": "employees_registered_at",
    "hjemmeside": "website", "epostadresse": "email", "telefon": "phone", "mobil": "mobile",
    "forretningsadresse.adresse": "business_street", "forretningsadresse.postnummer": "business_postcode",
    "forretningsadresse.poststed": "business_city", "forretningsadresse.kommune": "municipality",
    "forretningsadresse.kommunenummer": "municipality_number", "forretningsadresse.land": "country",
    "postadresse.adresse": "postal_street", "postadresse.postnummer": "postal_postcode", "postadresse.poststed": "postal_city",
    "institusjonellSektorkode.beskrivelse": "sector", "sisteInnsendteAarsregnskap": "latest_submitted_accounts",
    "registreringsdatoenhetsregisteret": "registered_at", "stiftelsesdato": "founded_at",
    "registrertIMvaRegisteret": "vat_registered", "registrertIForetaksregisteret": "in_business_register",
    "registreringsdatoForetaksregisteret": "business_registered_at",
    "registrertIFrivillighetsregisteret": "in_voluntary_register", "konkurs": "bankrupt", "konkursdato": "bankrupt_at",
    "underAvvikling": "liquidating", "underAvviklingDato": "liquidating_at",
    "underTvangsavviklingEllerTvangsopplosning": "forced_dissolution", "maalform": "language",
    "vedtektsdato": "articles_dated", "vedtektsfestetFormaal": "statutory_purpose", "aktivitet": "activity",
    "kapital.belop": "share_capital", "kapital.valuta": "share_capital_currency", "kapital.type": "share_capital_type",
    "kapital.innfortDato": "share_capital_registered_at", "overordnetEnhet": "parent_public_unit",
}


def load_entities(path: Path, wanted: set[str], registry: Registry) -> None:
    with _open_text(path) as handle:
        reader = csv.reader(handle)
        header = next(reader)
        index = {name: i for i, name in enumerate(header)}
        i_org, i_web, i_mail = index["organisasjonsnummer"], index.get("hjemmeside"), index.get("epostadresse")
        selected = [(index[c], key) for c, key in ENTITY_COLUMNS.items() if c in index]
        for row in reader:
            if len(row) < len(header):
                continue
            org = row[i_org]
            if i_web is not None and row[i_web]:
                registry._own(host_key(row[i_web]), org)
            if i_mail is not None and row[i_mail]:
                registry._own(host_key(row[i_mail]), org)
            if org in wanted:
                registry.entities[org] = {key: row[i] for i, key in selected}


def load_subunits(path: Path, wanted: set[str], registry: Registry) -> None:
    with _open_text(path) as handle:
        reader = csv.reader(handle)
        header = next(reader)
        index = {name: i for i, name in enumerate(header)}
        i_parent = index["overordnetEnhet"]
        cols = {
            "organisation_number": "organisasjonsnummer", "name": "navn", "industry_code": "naeringskode1.kode",
            "industry_label": "naeringskode1.beskrivelse", "employees": "antallAnsatte", "website": "hjemmeside",
            "email": "epostadresse", "phone": "telefon", "street": "beliggenhetsadresse.adresse",
            "postcode": "beliggenhetsadresse.postnummer", "city": "beliggenhetsadresse.poststed",
            "municipality": "beliggenhetsadresse.kommune", "started_at": "oppstartsdato",
            "registered_at": "registreringsdatoIEnhetsregisteret", "closed_at": "nedleggelsesdato",
        }
        cols = {k: index[v] for k, v in cols.items() if v in index}
        i_web, i_mail = index.get("hjemmeside"), index.get("epostadresse")
        for row in reader:
            if len(row) < len(header):
                continue
            parent = row[i_parent]
            if not parent:
                continue
            if i_web is not None and row[i_web]:
                registry._own(host_key(row[i_web]), parent)
            if i_mail is not None and row[i_mail]:
                registry._own(host_key(row[i_mail]), parent)
            if parent in wanted:
                registry.subunits.setdefault(parent, []).append({k: row[i] for k, i in cols.items()})


_ORG_LINE = re.compile(r'^ {1,3}"organisasjonsnummer"\s*:\s*"(\d{9})"')


def load_roles(path: Path, wanted: set[str], registry: Registry) -> None:
    """Stream the pretty-printed roles array; only decode objects for organisations in the batch.

    Layout (Jackson pretty printer): a top-level object opens with an indented "{" line and closes
    with a line starting at column 0 ("}," or "}"); nested objects are always indented.
    """
    wanted_bytes = {w.encode() for w in wanted}
    marker = b'\n  "organisasjonsnummer" : "'  # top-level key: exactly two spaces of indentation
    raw = path.open("rb")
    magic = raw.read(2)
    raw.seek(0)
    stream = gzip.GzipFile(fileobj=raw) if magic == b"\x1f\x8b" else raw
    tail = b""
    try:
        while True:
            chunk = stream.read(32 << 20)
            data = (tail + chunk).replace(b"\r\n", b"\n") if b"\r" in chunk[:4096] else tail + chunk
            if not data:
                break
            pos = 0
            while True:
                i = data.find(marker, pos)
                if i == -1:
                    break
                end = data.find(b"\n}", i)  # top-level object closes at column 0
                if end == -1:
                    break
                org = data[i + len(marker):i + len(marker) + 9]
                if org in wanted_bytes:
                    start = data.rfind(b"\n  {", 0, i)
                    try:
                        body = json.loads(data[start + 1:end + 2].decode("utf-8"))
                        registry.roles[body["organisasjonsnummer"]] = body
                    except (json.JSONDecodeError, KeyError, UnicodeDecodeError):
                        pass
                pos = end + 2
            if not chunk or len(registry.roles) == len(wanted):
                break
            tail = data[pos:]
    finally:
        raw.close()


def load_registry(http: Http, wanted: Iterable[str], cache_dir: Path, max_age_hours: float = 20, provided_dir: Path | None = None,
                  log=print, entity_file: Path | None = None) -> Registry:
    registry = Registry()
    wanted = set(wanted)
    for kind, loader in (("entities", load_entities), ("subunits", load_subunits), ("roles", load_roles)):
        started = time.monotonic()
        source = entity_file if (kind == "entities" and entity_file) else provided_dir
        path, meta = ensure_bulk(http, cache_dir, kind, max_age_hours, source)
        if path is None:
            registry.errors.append(f"{kind}: {meta.get('error')}")
            registry.sources[kind] = meta
            log(f"[registry] {kind}: bulk unavailable ({meta.get('error')})")
            continue
        try:
            loader(path, wanted, registry)
            meta["sha256"] = file_sha256(path)
        except Exception as exc:  # corrupt download etc. -> per-company fallback handles it
            registry.errors.append(f"{kind}: {type(exc).__name__}: {exc}")
            meta["error"] = f"parse_failed: {type(exc).__name__}"
        if kind == "entities" and not registry.entities and wanted and meta.get("origin") == "evaluator_supplied_snapshot":
            # Unrecognised supplied snapshot format: fall back to the official bulk download.
            registry.errors.append(f"entities: supplied snapshot {path} yielded no rows; using official bulk download")
            path, meta = ensure_bulk(http, cache_dir, kind, max_age_hours, None)
            if path is not None:
                try:
                    loader(path, wanted, registry)
                    meta["sha256"] = file_sha256(path)
                except Exception as exc:
                    meta["error"] = f"parse_failed: {type(exc).__name__}"
        registry.sources[kind] = meta
        log(f"[registry] {kind}: {meta.get('origin')} parsed in {time.monotonic() - started:.0f}s")
    return registry


# ---------------------------------------------------------------- per-company fallbacks

def _flat_entity(body: dict) -> dict:
    def get(*path):
        value = body
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        return value

    address = get("forretningsadresse") or {}
    postal = get("postadresse") or {}
    capital = get("kapital") or {}
    return {
        "organisation_number": body.get("organisasjonsnummer", ""), "name": body.get("navn", ""),
        "legal_form": get("organisasjonsform", "kode") or "", "legal_form_label": get("organisasjonsform", "beskrivelse") or "",
        "industry_code": get("naeringskode1", "kode") or "", "industry_label": get("naeringskode1", "beskrivelse") or "",
        "industry2_code": get("naeringskode2", "kode") or "", "industry2_label": get("naeringskode2", "beskrivelse") or "",
        "employees": str(body.get("antallAnsatte") or ""), "has_registered_employees": str(body.get("harRegistrertAntallAnsatte", "")).lower(),
        "employees_registered_at": body.get("registreringsdatoAntallAnsatteEnhetsregisteret", ""),
        "website": body.get("hjemmeside", ""), "email": body.get("epostadresse", ""), "phone": body.get("telefon", ""),
        "mobile": body.get("mobil", ""), "business_street": ", ".join(address.get("adresse") or []),
        "business_postcode": address.get("postnummer", ""), "business_city": address.get("poststed", ""),
        "municipality": address.get("kommune", ""), "municipality_number": address.get("kommunenummer", ""),
        "country": address.get("land", ""), "postal_street": ", ".join(postal.get("adresse") or []),
        "postal_postcode": postal.get("postnummer", ""), "postal_city": postal.get("poststed", ""),
        "sector": get("institusjonellSektorkode", "beskrivelse") or "",
        "latest_submitted_accounts": body.get("sisteInnsendteAarsregnskap", ""),
        "registered_at": body.get("registreringsdatoEnhetsregisteret", ""), "founded_at": body.get("stiftelsesdato", ""),
        "vat_registered": str(body.get("registrertIMvaregisteret", "")).lower(),
        "in_business_register": str(body.get("registrertIForetaksregisteret", "")).lower(),
        "business_registered_at": body.get("registreringsdatoForetaksregisteret", ""),
        "bankrupt": str(body.get("konkurs", "")).lower(), "bankrupt_at": body.get("konkursdato", ""),
        "liquidating": str(body.get("underAvvikling", "")).lower(), "liquidating_at": body.get("underAvviklingDato", ""),
        "forced_dissolution": str(body.get("underTvangsavviklingEllerTvangsopplosning", "")).lower(),
        "language": body.get("maalform", ""), "articles_dated": body.get("vedtektsdato", ""),
        "statutory_purpose": " ".join(body.get("vedtektsfestetFormaal") or []), "activity": " ".join(body.get("aktivitet") or []),
        "share_capital": str(capital.get("belop") or ""), "share_capital_currency": capital.get("valuta", ""),
        "share_capital_type": capital.get("type", ""), "share_capital_registered_at": capital.get("innfortDato", ""),
        "parent_public_unit": body.get("overordnetEnhet", ""),
    }


def fetch_entity_live(http: Http, org: str) -> tuple[dict | None, FetchResult]:
    res = http.get(ENTITY_API.format(org=org), "official_registry_live", accept="application/json")
    if res.ok:
        try:
            return _flat_entity(res.json()), res
        except ValueError:
            pass
    return None, res


def fetch_roles_live(http: Http, org: str) -> tuple[dict | None, FetchResult]:
    res = http.get(ROLES_API.format(org=org), "official_roles_live", accept="application/json")
    if res.ok:
        try:
            return res.json(), res
        except ValueError:
            pass
    return None, res


def fetch_subunits_live(http: Http, org: str) -> tuple[list[dict] | None, FetchResult]:
    res = http.get(SUBUNITS_API.format(org=org), "official_subunits_live", accept="application/json")
    if not res.ok:
        return None, res
    try:
        rows = (res.json().get("_embedded") or {}).get("underenheter") or []
    except ValueError:
        return None, res
    out = []
    for item in rows:
        address = item.get("beliggenhetsadresse") or {}
        out.append({
            "organisation_number": item.get("organisasjonsnummer", ""), "name": item.get("navn", ""),
            "industry_code": (item.get("naeringskode1") or {}).get("kode", ""),
            "industry_label": (item.get("naeringskode1") or {}).get("beskrivelse", ""),
            "employees": str(item.get("antallAnsatte") or ""), "website": item.get("hjemmeside", ""),
            "email": item.get("epostadresse", ""), "phone": item.get("telefon", ""),
            "street": ", ".join(address.get("adresse") or []), "postcode": address.get("postnummer", ""),
            "city": address.get("poststed", ""), "municipality": address.get("kommune", ""),
            "started_at": item.get("oppstartsdato", ""), "registered_at": item.get("registreringsdatoEnhetsregisteret", ""),
            "closed_at": item.get("nedleggelsesdato", ""),
        })
    return out, res


def now_iso() -> str:
    return utc_now()
