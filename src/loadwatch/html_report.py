"""The report as one self-contained HTML page: no external files, so it opens from disk and goes in an email. Everything
that came from a server is escaped: job names and messages are other people's text."""
from __future__ import annotations

from html import escape

from . import gantt

STATES = {"failed": "× Failed", "missing": "! Did not run", "suspect": "? Suspect", "slow": "! Slow", "running": "Running",
          "recovered": "Recovered", "ok": "OK"}
LEGEND = [("ok", "Succeeded"), ("slow", "! Slow"), ("suspect", "? Suspect"), ("failed", "× Failed"), ("canceled", "× Canceled"), ("running", "Running")]
CSS = """
:root{color-scheme:light;--surface:#fcfcfb;--page:#f9f9f7;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;
--band:rgba(11,11,11,.025);--line:rgba(11,11,11,.10);--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;--run:#2a78d6;--fill:#c3c2b7}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--surface:#1a1a19;--page:#0d0d0d;--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;
--band:rgba(255,255,255,.03);--line:rgba(255,255,255,.10);--run:#3987e5;--fill:#52514e}}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}h1{font-size:26px;line-height:1.25;margin:0 0 4px}h2{font-size:17px;margin:32px 0 10px}
.sub{color:var(--ink2);margin:0}.start{margin:12px 0 0;font-size:16px}section.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
ul.alerts{list-style:none;margin:0;padding:0}ul.alerts li{padding:10px 0;border-top:1px solid var(--line);overflow-wrap:anywhere}ul.alerts li:first-child{border-top:0;padding-top:0}
ul.alerts li:last-child{padding-bottom:0}.subject{font-weight:600}code{font:13px ui-monospace,Consolas,monospace;color:var(--ink2);display:block;margin-top:4px}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;color:var(--ink2);font-size:13px;margin-bottom:8px}.legend span{display:inline-flex;align-items:center;gap:6px}
.key{width:18px;height:10px;border-radius:3px;display:inline-block}.scroll{overflow-x:auto}
svg.gantt{display:block;width:100%;min-width:900px;height:auto;font:12px system-ui,-apple-system,"Segoe UI",sans-serif}
svg .grid{stroke:var(--grid);stroke-width:1}svg .axis{stroke:var(--axis)}svg .tick{fill:var(--muted);font-size:11px}svg .name{fill:var(--ink)}
svg .band{fill:var(--band)}svg .absent{fill:var(--ink2);font-weight:600}svg .icon{fill:var(--ink);font-weight:700}svg .hit{fill:transparent;cursor:default;outline:none}
svg .hit:focus{stroke:var(--ink);stroke-width:1.5}svg .usual{stroke:var(--ink);stroke-width:1.5}svg .now{stroke:var(--ink2);stroke-dasharray:3 3}
svg .nowlabel{fill:var(--ink2);font-size:11px;font-weight:600}svg .due{fill:var(--surface);stroke:var(--ink);stroke-width:1.5}svg .due.missed{fill:var(--critical)}svg .strip{fill:var(--fill)}
.ok{fill:var(--good);background:var(--good)}.slow{fill:var(--warning);background:var(--warning)}.suspect{fill:var(--serious);background:var(--serious)}
.failed{fill:var(--critical);background:var(--critical)}.canceled{fill:var(--muted);background:var(--muted)}.running{fill:var(--run);background:var(--run)}
.tip{position:fixed;display:none;pointer-events:none;max-width:340px;background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:8px 10px;
font-size:13px;box-shadow:0 2px 10px rgba(0,0,0,.18);z-index:2}.tip b{display:block}.tip span{display:block;color:var(--ink2)}
table{border-collapse:collapse;width:100%;font-size:14px}th{text-align:left;color:var(--ink2);font-weight:600}th,td{padding:6px 12px 6px 0;border-top:1px solid var(--line);vertical-align:top}
tr:first-child th{border-top:0}td.n{white-space:nowrap;font-variant-numeric:tabular-nums}.foot{color:var(--muted);font-size:13px;margin-top:24px}
@media print{.tip{display:none}body{background:#fff}}@media (forced-colors:active){svg .bar{stroke:CanvasText}}
"""
SCRIPT = """
(function(){var t=document.getElementById('tip');function show(e,r){var p=r.getAttribute('data-tip').split('|');t.textContent='';
p.forEach(function(x,i){var n=document.createElement(i?'span':'b');n.textContent=x;t.appendChild(n)});t.style.display='block';
var b=r.getBoundingClientRect(),x=(e&&e.clientX)||b.left,y=(e&&e.clientY)||b.top;t.style.left=Math.min(x+12,innerWidth-t.offsetWidth-8)+'px';
t.style.top=Math.max(8,y-t.offsetHeight-12)+'px'}
document.querySelectorAll('.hit').forEach(function(r){r.addEventListener('pointermove',function(e){show(e,r)});r.addEventListener('focus',function(){show(null,r)});
['pointerleave','blur'].forEach(function(n){r.addEventListener(n,function(){t.style.display='none'})})})})();
"""


def _alerts(alerts: list[dict]) -> str:
    items = []
    for a in alerts:
        cmd = f"<code>{escape(a['next'])}</code>" if a.get("next") else ""
        note = f"<div class=sub>Acknowledged: {escape(a['ack_note'])}</div>" if a.get("ack_note") else ""
        items.append(f'<li><span class="subject">[{escape(a["env"])}] {escape(a["subject"])}</span><br>{escape(a["message"])}{cmd}{note}</li>')
    return f'<section class="card"><ul class="alerts">{"".join(items)}</ul></section>'


def _table(doc: dict) -> str:
    rows = []
    for a in doc["activities"]:
        last = a["runs"][0] if a["runs"] else {}
        count = f"{len(a['runs'])}{'+' if a['more_runs'] else ''}"
        rows.append(f'<tr><td class="n">{STATES.get(a["state"], escape(a["state"]))}</td><td>{escape(a["name"])}</td><td class="n">{count}</td>'
                    f'<td class="n">{escape((last.get("start") or "-").replace("T", " "))}</td><td class="n">{gantt.clock(last.get("duration_s"))}</td>'
                    f'<td class="n">{gantt.clock(a["usual_s"])}</td><td>{escape(a["what"])}</td></tr>')
    head = "".join(f"<th>{h}</th>" for h in ("State", "Name", "Runs", "Last start", "Took", "Usual", "What it runs"))
    return f'<section class="card scroll"><table><tr>{head}</tr>{"".join(rows)}</table></section>'


def page(doc: dict) -> str:
    from .report import HEADINGS, listed, looked_at, summary
    parts = [f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
             f'<title>{escape(doc["verdict"])}</title><style>{CSS}</style><main><h1>{escape(doc["verdict"])}</h1>'
             f'<p class="sub">{escape(summary(doc))}</p><p class="sub">{escape(looked_at(doc))}</p>']
    if doc.get("start_here"):
        parts.append(f'<p class="start"><b>Start here: {escape(doc["start_here"])}</b>: {escape(doc["start_because"])}.</p>'
                     f'<p class="sub">Everything that needs attention: {escape(listed(doc))}.</p>')
    for severity, title in HEADINGS.items():
        found = [a for a in doc["attention"] if a["severity"] == severity]
        if found:
            parts.append(f"<h2>{title} ({len(found)})</h2>{_alerts(found)}")
    if doc["acknowledged"]:
        parts.append(f"<h2>Acknowledged ({len(doc['acknowledged'])})</h2>{_alerts(doc['acknowledged'])}")
    t = doc["timeline"]
    if t["rows"]:
        keys = "".join(f'<span><i class="key {k}"></i>{label}</span>' for k, label in LEGEND)
        keys += ('<span><svg width="10" height="14"><path d="M5 0v14" stroke="currentColor" stroke-width="1.5"/></svg>Usual duration</span>'
                 '<span><svg width="14" height="14"><path d="M7 1l5 6-5 6-5-6z" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>Deadline (filled: missed)</span>')
        parts.append(f'<h2>The period</h2><section class="card"><div class="legend">{keys}</div><div class="scroll">{gantt.svg(t)}</div></section>')
    parts.append(f"<h2>What ran ({doc['ran']})</h2>{_table(doc)}")
    if doc["resolved"]:
        items = "".join(f"<li>[{escape(a['env'])}] {escape(a['rule'])}: {escape(a['subject'])}</li>" for a in doc["resolved"])
        parts.append(f'<h2>Resolved today ({len(doc["resolved"])})</h2><section class="card"><ul class="alerts">{items}</ul></section>')
    blind = [f"{s['source']}: could not read {part}: {why}" for s in doc["sources"] for part, why in s.get("unread", {}).items()]
    if blind:
        parts.append('<h2>Not read</h2><section class="card"><ul class="alerts">' + "".join(f"<li>{escape(b)}</li>" for b in blind) + "</ul></section>")
    others = [f"{t['name']}: {t['why']}" for t in doc.get("not_watched", [])] + list(doc.get("notes", []))
    if others:
        parts.append('<h2>Not watched</h2><section class="card"><ul class="alerts">' + "".join(f"<li>{escape(o)}</li>" for o in others) + "</ul></section>")
    where = ", ".join(f"{s['source']} ({s['location'] or 'no answer'}, watching {s['watched']})" for s in doc["sources"])
    parts.append(f'<p class="foot">Sources: {escape(where)}.</p></main><div class="tip" id="tip"></div><script>{SCRIPT}</script></html>')
    return "".join(parts)
