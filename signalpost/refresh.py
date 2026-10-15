"""Refresh as a diff: stable claim keys, preserved history, typed material changes, idempotent reruns."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

MATERIAL_FIELDS = {
    "legal_name", "legal_form", "registry_status", "industry", "business_address", "registered_employees", "share_capital",
    "ceo", "board_chair", "board_member", "auditor", "owner", "general_partner", "official_website", "registered_workplace",
    "revenue", "operating_result", "net_result", "equity", "total_assets", "total_debt", "annual_accounts_filing",
    "latest_filed_accounts_year", "job_posting", "news_item", "social_profile",
}
TYPED = {
    ("ceo", "added"): "new_role", ("ceo", "removed"): "removed_role", ("board_chair", "added"): "new_role",
    ("board_chair", "removed"): "removed_role", ("board_member", "added"): "new_role", ("board_member", "removed"): "removed_role",
    ("auditor", "added"): "new_role", ("auditor", "removed"): "removed_role",
    ("annual_accounts_filing", "added"): "new_filing", ("latest_filed_accounts_year", "changed"): "new_filing",
    ("job_posting", "added"): "new_job", ("job_posting", "removed"): "closed_job",
    ("registered_workplace", "added"): "new_location", ("registered_workplace", "removed"): "closed_location",
    ("news_item", "added"): "new_activity", ("official_website", "changed"): "website_changed",
    ("official_website", "added"): "website_found", ("registry_status", "changed"): "status_changed",
    ("website_description", "changed"): "changed_description", ("legal_name", "changed"): "name_changed",
    ("business_address", "changed"): "address_changed", ("registered_employees", "changed"): "employee_count_changed",
    ("social_profile", "added"): "new_profile", ("social_profile", "removed"): "removed_profile",
}
# Fields observed only from volatile web sources: a missing observation is not a removal unless the source was re-read.
WEB_FIELDS = {"job_posting", "news_item", "social_profile", "website_contact_email", "website_contact_phone", "careers_page",
              "recruiting_system_link", "leader_named_on_website", "website_address", "public_brand_name", "website_description",
              "official_website", "website_founding_date", "website_employee_statement"}


def value_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def claim_id(claim: dict) -> str:
    return f"{claim['section']}|{claim['key']}"


class StateStore:
    def __init__(self, root: Path):
        self.root = root
        (root / "companies").mkdir(parents=True, exist_ok=True)

    def path(self, org: str) -> Path:
        return self.root / "companies" / f"{org}.json"

    def load(self, org: str) -> dict | None:
        path = self.path(org)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                return None
        return None

    def save(self, org: str, state: dict) -> None:
        path = self.path(org)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(path)


def state_from_envelope(envelope: dict) -> dict:
    """Build a minimal previous state from a previously emitted envelope (for --previous input)."""
    claims = {}
    evidence = {e["id"]: e for e in envelope.get("evidence", [])}
    run = envelope.get("run", {})
    for c in envelope.get("claims", []):
        if c.get("availability") != "available" or c.get("stale"):
            continue
        claims[claim_id(c)] = {"claim": c, "hash": value_hash(c["value"]), "first_seen": c.get("first_seen") or run.get("completed_at"),
                               "last_seen": run.get("completed_at"), "evidence": [evidence[e] for e in c["evidence_ids"] if e in evidence]}
    return {"organisation_number": envelope.get("organisation_number"), "last_run_id": run.get("run_id"), "claims": claims, "history": []}


def apply_refresh(env, previous: dict | None, now: str, web_checked: bool) -> tuple[list[dict], dict]:
    """Diff current claims with the previous state; carry forward values whose refresh failed; return (changes, new_state)."""
    previous = previous or {"claims": {}, "history": []}
    prev_claims = previous.get("claims", {})
    current = {claim_id(c): c for c in env.claims if c["availability"] == "available"}
    field_states: dict[tuple[str, str], set[str]] = {}
    for c in env.claims:
        field_states.setdefault((c["section"], c["field"]), set()).add(c["availability"])
    changes = []
    new_claims = {}
    for cid, c in current.items():
        h = value_hash(c["value"])
        old = prev_claims.get(cid)
        first_seen = old["first_seen"] if old else now
        c["first_seen"] = first_seen
        c["last_seen"] = now
        new_claims[cid] = {"claim": {k: v for k, v in c.items() if k not in ("first_seen", "last_seen")}, "hash": h,
                           "first_seen": first_seen, "last_seen": now, "evidence": [env.evidence[e] for e in c["evidence_ids"] if e in env.evidence]}
        if old is None:
            if prev_claims:
                changes.append(_change(c, "added", None, c["value"], [], c["evidence_ids"]))
        elif old["hash"] != h:
            old_ev = [e["id"] for e in old.get("evidence", [])]
            for e in old.get("evidence", []):
                env.evidence.setdefault(e["id"], dict(e, superseded=True))
            changes.append(_change(c, "changed", old["claim"].get("value"), c["value"], old_ev, c["evidence_ids"]))
    for cid, old in prev_claims.items():
        if cid in current:
            continue
        claim = old["claim"]
        states = field_states.get((claim["section"], claim["field"]), set())
        refresh_failed = bool(states & {"failed", "blocked"}) or (claim["field"] in WEB_FIELDS and not web_checked)
        for e in old.get("evidence", []):
            env.evidence.setdefault(e["id"], dict(e, superseded=not refresh_failed))
        if refresh_failed:
            # Keep the last supported value, clearly marked as not re-confirmed in this run.
            carried = dict(claim, stale=True, first_seen=old["first_seen"], last_seen=old["last_seen"],
                           note=f"Carried forward: refresh of this source failed or was not attempted in this run; last confirmed {old['last_seen']}.")
            env.claims.append(carried)
            new_claims[cid] = dict(old)
        else:
            changes.append(_change(claim, "removed", claim.get("value"), None, [e["id"] for e in old.get("evidence", [])], []))
    history = list(previous.get("history", []))
    if changes or not history:
        history.append({"run_id": env.run_id, "at": now, "changes": len(changes)})
    state = {"organisation_number": env.org, "last_run_id": env.run_id, "claims": new_claims, "history": history[-50:]}
    return changes, state


def _change(claim: dict, kind: str, old, new, old_ev, new_ev) -> dict:
    field = claim["field"]
    return {
        "key": claim["key"],
        "section": claim["section"],
        "field": field,
        "change_type": TYPED.get((field, kind), f"{kind}_value" if kind == "changed" else f"{kind}_claim"),
        "diff": kind,
        "material": field in MATERIAL_FIELDS,
        "previous_value": old,
        "current_value": new,
        "previous_evidence_ids": old_ev,
        "current_evidence_ids": new_ev,
    }
