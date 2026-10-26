"""Official annual accounts from Regnskapsregisteret (one request returns the latest three years)."""
from __future__ import annotations

from .http import FetchResult, Http

ACCOUNTS_API = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
ACCOUNT_COPY = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/{year}"

# (claim field, path in the official JSON, label)
FIELDS = [
    ("revenue", ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"), "Total operating revenue"),
    ("sales_revenue", ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "salgsinntekter"), "Sales revenue"),
    ("operating_costs", ("resultatregnskapResultat", "driftsresultat", "driftskostnad", "sumDriftskostnad"), "Total operating costs"),
    ("payroll_costs", ("resultatregnskapResultat", "driftsresultat", "driftskostnad", "loennskostnad"), "Payroll costs"),
    ("operating_result", ("resultatregnskapResultat", "driftsresultat", "driftsresultat"), "Operating result"),
    ("net_financial_items", ("resultatregnskapResultat", "finansresultat", "nettoFinans"), "Net financial items"),
    ("profit_before_tax", ("resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"), "Profit before tax"),
    ("tax_expense", ("resultatregnskapResultat", "ordinaertResultatSkattekostnad"), "Tax expense"),
    ("net_result", ("resultatregnskapResultat", "aarsresultat"), "Net result for the year"),
    ("total_assets", ("eiendeler", "sumEiendeler"), "Total assets"),
    ("fixed_assets", ("eiendeler", "anleggsmidler", "sumAnleggsmidler"), "Fixed assets"),
    ("current_assets", ("eiendeler", "omloepsmidler", "sumOmloepsmidler"), "Current assets"),
    ("receivables", ("eiendeler", "sumFordringer"), "Receivables"),
    ("cash", ("eiendeler", "sumBankinnskuddOgKontanter"), "Bank deposits and cash"),
    ("equity", ("egenkapitalGjeld", "egenkapital", "sumEgenkapital"), "Total equity"),
    ("paid_in_equity", ("egenkapitalGjeld", "egenkapital", "innskuttEgenkapital", "sumInnskuttEgenkaptial"), "Paid-in equity"),
    ("retained_earnings", ("egenkapitalGjeld", "egenkapital", "opptjentEgenkapital", "sumOpptjentEgenkapital"), "Retained earnings"),
    ("total_debt", ("egenkapitalGjeld", "gjeldOversikt", "sumGjeld"), "Total debt"),
    ("short_term_debt", ("egenkapitalGjeld", "gjeldOversikt", "kortsiktigGjeld", "sumKortsiktigGjeld"), "Short-term debt"),
    ("long_term_debt", ("egenkapitalGjeld", "gjeldOversikt", "langsiktigGjeld", "sumLangsiktigGjeld"), "Long-term debt"),
]


def _get(obj, path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def parse_accounts(body) -> list[dict]:
    """Normalise every returned filing. Absent fields stay absent (never zero-filled)."""
    filings = []
    for item in body if isinstance(body, list) else []:
        period = item.get("regnskapsperiode") or {}
        start, end = period.get("fraDato"), period.get("tilDato")
        if not end:
            continue
        values = {}
        for field, path, label in FIELDS:
            value = _get(item, path)
            if isinstance(value, (int, float)):
                values[field] = {"value": value, "label": label, "path": ".".join(path)}
        filings.append({
            "year": end[:4],
            "period_start": start,
            "period_end": end,
            "currency": item.get("valuta"),
            "account_type": item.get("regnskapstype"),
            "is_parent_company": (item.get("virksomhet") or {}).get("morselskap"),
            "liquidation_accounts": item.get("avviklingsregnskap"),
            "statement_size": item.get("oppstillingsplan"),
            "small_enterprise_rules": (item.get("regnkapsprinsipper") or {}).get("smaaForetak"),
            "audit_waived": (item.get("revisjon") or {}).get("fravalgRevisjon"),
            "not_audited": (item.get("revisjon") or {}).get("ikkeRevidertAarsregnskap"),
            "journal_number": item.get("journalnr"),
            "record_id": item.get("id"),
            "values": values,
        })
    filings.sort(key=lambda f: (f["period_end"], f["account_type"] or ""), reverse=True)
    return filings


def fetch_accounts(http: Http, org: str, reserved: bool | str = "accounts") -> tuple[list[dict] | None, FetchResult]:
    res = http.get(ACCOUNTS_API.format(org=org), "official_annual_accounts", accept="application/json", reserved=reserved, timeout=20)
    transient = res.status in (500, 502, 503, 504, 429) or (res.status == 0 and res.error not in (None, "budget_exhausted", "unsafe_or_unresolvable_url"))
    if transient and not res.blocked:
        # one retry on transient source/network errors (timeouts included)
        import time

        time.sleep(3 if res.status == 429 else 1)
        retry = http.get(ACCOUNTS_API.format(org=org), "official_annual_accounts", accept="application/json", timeout=30)
        if not retry.blocked:
            res = retry
    if res.ok:
        try:
            return parse_accounts(res.json()), res
        except ValueError:
            return None, res
    return None, res
