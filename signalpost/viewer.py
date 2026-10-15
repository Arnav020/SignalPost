"""Self-contained HTML viewer: find, compare and verify company facts on desktop and mobile."""
from __future__ import annotations

import html
import json
from pathlib import Path

FIELD_LABELS = {
    "legal_name": "Legal name", "organisation_number": "Organisation number", "legal_form": "Legal form", "registry_status": "Status",
    "industry": "Industry", "institutional_sector": "Sector", "business_address": "Business address", "postal_address": "Postal address",
    "founded_date": "Founded", "registered_date": "Registered", "in_business_register": "In Foretaksregisteret", "vat_registered": "VAT registered",
    "registered_employees": "Registered employees", "registry_phone": "Phone (registry)", "registry_email": "Email (registry)",
    "share_capital": "Share capital", "statutory_purpose": "Statutory purpose", "registered_activity": "Registered activity",
    "public_brand_name": "Public brand", "website_description": "Website description", "ceo": "CEO", "board_chair": "Board chair",
    "board_member": "Board member", "board_deputy_chair": "Deputy chair", "board_deputy_member": "Deputy board member", "auditor": "Auditor",
    "accountant": "Accountant", "owner": "Owner", "contact_person": "Contact person", "general_partner": "Partner", "role": "Role",
    "registered_workplace": "Registered workplace", "leader_named_on_website": "Leader named on website", "official_website": "Official website",
    "social_profile": "Social profile", "website_contact_email": "Email (website)", "website_contact_phone": "Phone (website)",
    "website_address": "Address (website)", "careers_page": "Careers page", "recruiting_system_link": "Recruiting system",
    "job_posting": "Job posting", "news_item": "News / activity", "registry_event": "Registry event", "annual_accounts_filing": "Filing",
    "latest_filed_accounts_year": "Latest filed accounts",
}
FIN_ORDER = ["revenue", "operating_result", "profit_before_tax", "net_result", "total_assets", "equity", "total_debt", "cash", "payroll_costs"]


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        if "platform" in value:
            return f"{value['platform']}: {value.get('url')}"
        if "name" in value and "role" in value:
            return f"{value['name']} ({value['role']})" if value.get("role") else value["name"]
        if "nace" in value:
            return f"{value['nace']} {value.get('label') or ''}".strip()
        if "street" in value:
            return ", ".join(str(v) for v in (value.get("street"), " ".join(filter(None, [value.get("postcode"), value.get("city")]))) if v)
        if "title" in value:
            return value.get("title") or value.get("url") or ""
        if "event" in value:
            return value.get("description") or value["event"]
        if "amount" in value:
            return f"{value['amount']:,.0f} {value.get('currency') or ''}".replace(",", " ")
        if "code" in value:
            return f"{value.get('label') or ''} ({value['code']})"
        if "year" in value:
            return f"{value['year']} {value.get('account_type') or ''}".strip()
        return ", ".join(f"{k}: {v}" for k, v in value.items() if v not in (None, "", []))
    return str(value)


def compact(envelope: dict) -> dict:
    evidence = {e["id"]: e for e in envelope.get("evidence", [])}
    facts = []
    financial = {}
    for c in envelope.get("claims", []):
        ev = [evidence[e] for e in c.get("evidence_ids", []) if e in evidence]
        src = [{"u": e.get("source_url"), "t": (e.get("retrieved_at") or "")[:10], "c": e.get("source_class"), "s": (e.get("claim_span") or "")[:160]} for e in ev[:2]]
        if c["section"] == "annual_accounts" and c["field"] in FIN_ORDER and c["availability"] == "available":
            year = c["reporting_period"]["end"][:4]
            if (c.get("account_type") or "SELSKAP") == "SELSKAP":
                financial.setdefault(c["field"], {})[year] = {"v": c["value"], "src": src[:1]}
            continue
        if c["section"] == "annual_accounts" and c["field"] not in ("annual_accounts", "latest_filed_accounts_year", "annual_accounts_filing"):
            continue
        facts.append({
            "s": c["section"], "f": c["field"], "l": FIELD_LABELS.get(c["field"], c["field"].replace("_", " ").capitalize()),
            "v": _fmt(c.get("value")), "u": (c["value"].get("url") if isinstance(c.get("value"), dict) else (c["value"] if isinstance(c.get("value"), str) and c["value"].startswith("http") else None)),
            "a": c["availability"], "d": c.get("effective_date"), "r": c.get("reason"), "st": c.get("stale"), "src": src,
            "id": c.get("identity_level"),
        })
    reg = next((c for c in envelope.get("claims", []) if c["field"] == "business_address" and c["availability"] == "available"), None)
    ind = next((c for c in envelope.get("claims", []) if c["field"] == "industry" and c["availability"] == "available"), None)
    return {
        "o": envelope.get("organisation_number"), "n": envelope.get("legal_name") or envelope.get("input"),
        "city": (reg["value"].get("municipality") or reg["value"].get("city")) if reg else None,
        "ind": ind["value"].get("label") if ind else None,
        "st": envelope.get("state"),
        "sec": {k: v.get("state") for k, v in (envelope.get("sections") or {}).items()},
        "sum": (envelope.get("summary") or {}).get("statements") or [],
        "unk": (envelope.get("summary") or {}).get("unknowns") or [],
        "ch": [{"t": c["change_type"], "k": c["key"], "m": c.get("material"), "pv": _fmt(c.get("previous_value")), "cv": _fmt(c.get("current_value"))}
               for c in envelope.get("changes", [])][:40],
        "facts": facts, "fin": financial, "run": (envelope.get("run") or {}).get("completed_at"),
        "err": envelope.get("errors") or [],
    }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signalpost profiles</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e2dd;--accent:#1f5f8b;--ok:#2e7d4f;--warn:#a26a00;--bad:#b03a2e;--chip:#eef2f5}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141413;--panel:#1d1d1b;--ink:#ecebe6;--muted:#a3a29b;--line:#34332f;--accent:#7cb4dc;--ok:#7ccf9c;--warn:#e2b55a;--bad:#ef8c80;--chip:#262a2e}}
:root[data-theme="dark"]{--bg:#141413;--panel:#1d1d1b;--ink:#ecebe6;--muted:#a3a29b;--line:#34332f;--accent:#7cb4dc;--ok:#7ccf9c;--warn:#e2b55a;--bad:#ef8c80;--chip:#262a2e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:14px 16px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:2}
header h1{font-size:17px;margin:0 0 8px}header .meta{color:var(--muted);font-size:13px}
.controls{display:flex;gap:8px;flex-wrap:wrap}input[type=search]{flex:1;min-width:200px;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font-size:15px}
select,button{padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);font-size:14px;cursor:pointer}
main{display:grid;grid-template-columns:minmax(260px,360px) 1fr;min-height:calc(100vh - 100px)}
#list{border-right:1px solid var(--line);overflow:auto;max-height:calc(100vh - 100px)}
.item{padding:10px 14px;border-bottom:1px solid var(--line);cursor:pointer}.item:hover,.item.active{background:var(--chip)}
.item b{display:block;font-size:14px}.item small{color:var(--muted)}
.dots{display:flex;gap:4px;margin-top:4px}.dot{width:9px;height:9px;border-radius:50%;background:var(--line)}.dot.available{background:var(--ok)}.dot.ambiguous,.dot.blocked{background:var(--warn)}.dot.failed{background:var(--bad)}
#detail{padding:18px 22px;overflow:auto;max-height:calc(100vh - 100px)}
h2{margin:0 0 2px;font-size:22px}h3{font-size:15px;margin:22px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.sum p{margin:6px 0}.sum sup a{font-size:11px;color:var(--accent)}
.fact{display:grid;grid-template-columns:180px 1fr;gap:8px;padding:6px 0;border-bottom:1px dashed var(--line)}
.fact .l{color:var(--muted);font-size:13px}.src{font-size:12px;color:var(--muted)}.src a{color:var(--accent)}
.state{font-size:12px;padding:1px 7px;border-radius:10px;background:var(--chip);color:var(--muted)}.state.available{color:var(--ok)}.state.ambiguous,.state.blocked{color:var(--warn)}.state.failed{color:var(--bad)}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}td:first-child,th:first-child{text-align:left}
a{color:var(--accent);word-break:break-word}.muted{color:var(--muted)}.chips{display:flex;gap:6px;flex-wrap:wrap}.chip{background:var(--chip);border-radius:12px;padding:2px 9px;font-size:12px}
#cmp{position:fixed;bottom:0;left:0;right:0;background:var(--panel);border-top:1px solid var(--line);padding:10px 16px;display:none;max-height:45vh;overflow:auto}
.back{display:none}
@media (max-width:760px){main{grid-template-columns:1fr}#list{max-height:none;border-right:0}#detail{display:none;max-height:none;padding:14px 16px}
 body.show-detail #list{display:none}body.show-detail #detail{display:block}.back{display:inline-block;margin-bottom:10px}.fact{grid-template-columns:1fr}}
</style></head><body>
<header><h1>Signalpost — Norwegian company profiles</h1>
<div class="controls"><input type="search" id="q" placeholder="Search name, org number, city, industry…" aria-label="Search">
<select id="flt" aria-label="Filter"><option value="">All companies</option><option value="web">Verified website</option><option value="jobs">Hiring signals</option><option value="fin">Accounts available</option><option value="chg">Changed since last run</option></select>
<button id="theme" title="Toggle theme">◐</button></div>
<div class="meta" id="meta"></div></header>
<main><div id="list" role="list"></div><div id="detail"><p class="muted">Select a company to see its evidence-backed profile.</p></div></main>
<div id="cmp"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);const L=document.getElementById('list'),T=document.getElementById('detail');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const SECS=[['legal_identity_and_brand','Identity & brand'],['annual_accounts','Annual accounts'],['leadership_and_workplaces','Leadership & workplaces'],['website_and_profiles','Website & profiles'],['hiring_and_activity','Hiring & activity']];
const cmp=new Set();let cur=null;
document.getElementById('meta').textContent=D.length+' companies · generated '+(D[0]?.run||'')+' · every fact links to its source';
function has(c,k){if(k==='web')return c.facts.some(f=>f.f==='official_website'&&f.a==='available');if(k==='jobs')return c.facts.some(f=>(f.f==='job_posting'||f.f==='careers_page')&&f.a==='available');if(k==='fin')return Object.keys(c.fin).length>0;if(k==='chg')return c.ch.length>0;return true}
function score(c){return c.facts.filter(f=>f.a==='available').length}
function renderList(){const q=document.getElementById('q').value.toLowerCase().trim(),k=document.getElementById('flt').value;
 const rows=D.filter(c=>has(c,k)&&(!q||[c.n,c.o,c.city,c.ind].join(' ').toLowerCase().includes(q))).sort((a,b)=>score(b)-score(a));
 L.innerHTML=rows.map(c=>`<div class="item ${cur===c.o?'active':''}" role="listitem" tabindex="0" data-o="${c.o}"><b>${esc(c.n)}</b><small>${esc(c.o)} · ${esc(c.city||'')} · ${esc(c.ind||'')}</small><div class="dots">${SECS.map(([s,l])=>`<span class="dot ${c.sec[s]||''}" title="${l}: ${c.sec[s]||'n/a'}"></span>`).join('')}</div></div>`).join('')||'<p class="muted" style="padding:14px">No matches.</p>';
 L.querySelectorAll('.item').forEach(e=>{e.onclick=()=>show(e.dataset.o);e.onkeydown=ev=>{if(ev.key==='Enter')show(e.dataset.o)}})}
function srcs(f){return f.src.map(s=>`<a href="${esc(s.u)}" target="_blank" rel="noopener" title="${esc(s.s)}">${esc((s.c||'source').replace(/_/g,' '))}</a> ${esc(s.t)}`).join(' · ')}
function money(v){const a=Math.abs(v);return (v<0?'-':'')+(a>=1e6?(a/1e6).toFixed(1)+' M':a>=1e3?(a/1e3).toFixed(0)+' k':a.toFixed(0))}
function show(o){const c=D.find(x=>x.o===o);cur=o;document.body.classList.add('show-detail');renderList();
 const ev={};c.facts.forEach(f=>f.src.forEach(s=>ev[s.u]=1));
 let h=`<button class="back" onclick="document.body.classList.remove('show-detail')">← Back</button><h2>${esc(c.n)}</h2><div class="muted">${esc(c.o)} · ${esc(c.city||'')} · ${esc(c.ind||'')} · <a href="https://virksomhet.brreg.no/nb/oppslag/enheter/${c.o}" target="_blank" rel="noopener">Brønnøysund record</a></div>
 <p><button onclick="toggleCmp('${c.o}')">${cmp.has(c.o)?'Remove from':'Add to'} comparison</button></p><div class="chips">${SECS.map(([s,l])=>`<span class="chip">${l}: <span class="state ${c.sec[s]}">${c.sec[s]||'n/a'}</span></span>`).join('')}</div>
 <h3>Summary</h3><div class="sum">${c.sum.map(s=>`<p>${esc(s.text)}</p>`).join('')}${c.unk.length?`<p class="muted"><b>Unknown:</b> ${c.unk.map(esc).join(' ')}</p>`:''}</div>`;
 const years=[...new Set(Object.values(c.fin).flatMap(x=>Object.keys(x)))].sort().reverse();
 if(years.length){h+=`<h3>Annual accounts (NOK)</h3><table><tr><th>Item</th>${years.map(y=>`<th>${y}</th>`).join('')}</tr>${Object.entries(c.fin).map(([k,v])=>`<tr><td>${esc(k.replace(/_/g,' '))}</td>${years.map(y=>`<td>${v[y]?`<a href="${esc(v[y].src[0]?.u)}" target="_blank" rel="noopener" title="Regnskapsregisteret">${money(v[y].v)}</a>`:'<span class=muted>–</span>'}</td>`).join('')}</tr>`).join('')}</table><p class="src">Source: official annual accounts, Regnskapsregisteret · filing copies: ${years.map(y=>`<a href="https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/${c.o}/${y}" target="_blank" rel="noopener">${y} PDF</a>`).join(' · ')}</p>`}
 for(const [s,l] of SECS){const fs=c.facts.filter(f=>f.s===s);if(!fs.length)continue;h+=`<h3>${l}</h3>`;
  for(const f of fs){const val=f.a==='available'?(f.u?`<a href="${esc(f.u)}" target="_blank" rel="noopener">${esc(f.v)}</a>`:esc(f.v)):`<span class="state ${f.a}">${f.a}</span> <span class="muted">${esc(f.r||'')}</span>`;
   h+=`<div class="fact"><div class="l">${esc(f.l)}${f.d?`<br><span class="src">${esc(f.d)}</span>`:''}</div><div>${val}${f.st?' <span class="state blocked">not re-confirmed</span>':''}${f.id?` <span class="chip">identity: ${esc(f.id)}</span>`:''}<div class="src">${srcs(f)}</div></div></div>`}}
 if(c.ch.length){h+=`<h3>Changes since previous run</h3>${c.ch.map(x=>`<div class="fact"><div class="l">${esc(x.t)}${x.m?' · material':''}</div><div>${esc(x.pv)} → ${esc(x.cv)}</div></div>`).join('')}`}
 h+=`<h3>Sources</h3><div class="src">${Object.keys(ev).filter(Boolean).map(u=>`<div><a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a></div>`).join('')}</div>`;
 T.innerHTML=h;T.scrollTop=0}
function toggleCmp(o){cmp.has(o)?cmp.delete(o):cmp.size<4&&cmp.add(o);show(o);renderCmp()}
function renderCmp(){const box=document.getElementById('cmp');if(!cmp.size){box.style.display='none';return}box.style.display='block';
 const cs=[...cmp].map(o=>D.find(x=>x.o===o));const latest=(c,k)=>{const v=c.fin[k];if(!v)return'–';const y=Object.keys(v).sort().pop();return money(v[y].v)+' ('+y+')'};
 const val=(c,f)=>c.facts.filter(x=>x.f===f&&x.a==='available').length;
 const rows=[['Revenue',c=>latest(c,'revenue')],['Net result',c=>latest(c,'net_result')],['Equity',c=>latest(c,'equity')],['Employees',c=>(c.facts.find(x=>x.f==='registered_employees'&&x.a==='available')||{}).v||'–'],['Website',c=>(c.facts.find(x=>x.f==='official_website'&&x.a==='available')||{}).v||'–'],['Workplaces',c=>val(c,'registered_workplace')],['Social profiles',c=>val(c,'social_profile')],['Job postings',c=>val(c,'job_posting')]];
 box.innerHTML=`<b>Compare</b> <button onclick="cmp.clear();renderCmp()">Clear</button><table><tr><th></th>${cs.map(c=>`<th>${esc(c.n)}</th>`).join('')}</tr>${rows.map(([l,fn])=>`<tr><td>${l}</td>${cs.map(c=>`<td>${esc(fn(c))}</td>`).join('')}</tr>`).join('')}</table>`}
document.getElementById('q').oninput=renderList;document.getElementById('flt').onchange=renderList;
document.getElementById('theme').onclick=()=>{const r=document.documentElement;r.dataset.theme=r.dataset.theme==='dark'?'light':'dark'};
renderList();const first=new URLSearchParams(location.hash.slice(1)).get('org');if(first)show(first);
</script></body></html>"""


def write_viewer(envelopes_path: Path, output: Path) -> None:
    rows = []
    for line in envelopes_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(compact(json.loads(line)))
    data = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    output.write_text(PAGE.replace("__DATA__", data), encoding="utf-8")


if __name__ == "__main__":
    import sys

    write_viewer(Path(sys.argv[1]), Path(sys.argv[2]))
    print(html.escape(sys.argv[2]))
