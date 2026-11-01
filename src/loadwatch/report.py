"""The morning report: everything that ran in the period, and what needs attention, most urgent first. Built from what
every source returns, so a new source shows up here without a change."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from . import gantt
from .activity import Result, priority, why_first

ORDER = ("failed", "missing", "suspect", "slow", "running", "recovered", "ok")
HEADINGS = {"critical": "Needs you now", "warning": "Needs a look", "info": "Worth knowing"}


_clock = gantt.clock


def _alert(a: dict) -> dict:
    return {k: a[k] for k in ("id", "env", "rule", "subject", "severity", "message", "next", "opened_at", "ack_note", "resolved_at", "at",
                              "in_a_row", "blocks", "person", "caused_by") if a.get(k) not in (None, [], 0)}


def build(results: list[Result], alerts: list[dict], since: datetime, now: datetime, estate=None) -> dict:
    """alerts: from the store, open and resolved, so that what was already known and acknowledged keeps that."""
    stamp = since.isoformat()[:19]
    current = [a for a in alerts if a["resolved_at"] is None]
    attention = sorted((a for a in current if a["acked_at"] is None), key=lambda a: priority(a, now))
    urgent = [a for a in attention if a["severity"] != "info"]
    activities = [x for r in results for x in r.activities]
    counts = {state: sum(a.state == state for a in activities) for state in ORDER}
    ran = len(activities) - counts["missing"]
    if not results:
        verdict = "Nothing to watch yet"
    elif urgent:
        verdict = f"{len(urgent)} thing{'s' if len(urgent) != 1 else ''} need{'' if len(urgent) != 1 else 's'} your attention"
    else:
        verdict = f"All clear: {ran} ran, nothing needs you"
    ordered = [asdict(a) for a in sorted(activities, key=lambda a: ORDER.index(a.state))]
    return {"verdict": verdict, "since": stamp, "at": now.isoformat()[:19], "counts": counts, "ran": ran,
            "sources": [{"source": r.source, "kind": r.kind, "location": r.location, "watched": r.watched,
                         "ran": len(r.activities), **({"unread": r.unread} if r.unread else {})} for r in results],
            "start_here": attention[0]["subject"] if urgent else None, "start_because": why_first(attention[0], now) if urgent else None,
            "attention": [_alert(a) for a in attention],
            "acknowledged": [_alert(a) for a in current if a["acked_at"] is not None],
            "resolved": [_alert(a) for a in alerts if a["resolved_at"] is not None and a["resolved_at"] >= stamp],
            "activities": ordered, "timeline": gantt.timeline(ordered, now),
            "not_watched": [{"kind": t.kind, "name": t.name, "why": t.why_not} for t in (estate.targets if estate else [])
                            if not t.watched and t.why_not != "nothing in it runs"],
            "notes": estate.notes if estate else [], "covered": covered(estate)}


def covered(estate) -> dict:
    """What was looked at, and what was not, so that "nothing needs you" says how far that goes."""
    targets = estate.targets if estate else []
    servers = [t.name for t in targets if t.kind == "sql_server" and t.watched]
    workspaces = [t.name for t in targets if t.kind == "fabric_workspace" and t.watched]
    idle = sum(t.kind == "fabric_workspace" and not t.watched for t in targets)
    if workspaces:
        fabric = f"{len(workspaces)} Fabric workspace{'s' if len(workspaces) != 1 else ''}"
    else:
        why = next((n for n in (estate.notes if estate else []) if n.startswith("Fabric")), None)
        fabric = why.rstrip(".") if why else f"no Fabric workspace with anything to watch ({idle} without)" if idle else (
            "no Fabric workspaces are visible to this login")
        if estate and not why and all("loadwatch.toml" in t.found_by for t in targets):
            fabric = "Fabric is not watched: loadwatch.toml lists what to watch"
    return {"sql_servers": servers, "fabric_workspaces": workspaces, "fabric": fabric}


def looked_at(doc: dict) -> str:
    c = doc["covered"]
    n = len(c["sql_servers"])
    sql = f"{n} SQL Server{'s' if n != 1 else ''}" + (f" ({', '.join(c['sql_servers'][:4])})" if n else "")
    return f"Looked at {sql}, and {c['fabric']}."


def estate_doc(estate) -> dict:
    """What discovery found: what is watched, and what is not and why."""
    return {"at": estate.at, "watched": [asdict(t) for t in estate.targets if t.watched],
            "not_watched": [asdict(t) for t in estate.targets if not t.watched], "notes": estate.notes}


def estate_text(doc: dict) -> str:
    names = {"sql_server": "SQL Server", "fabric_workspace": "Fabric workspace"}
    lines = [f"# Watching {len(doc['watched'])}", ""]
    for t in doc["watched"]:
        unit = "jobs" if t["kind"] == "sql_server" else "items"
        lines.append(f"- {names[t['kind']]} **{t['name']}**: {', '.join(t['can'])} ({t['size']} {unit}). Found by {', '.join(t['found_by'])}.")
    if not doc["watched"]:
        lines.append("Nothing yet.")
    if doc["not_watched"]:
        lines += ["", f"## Not watched ({len(doc['not_watched'])})", ""]
        lines += [f"- {names[t['kind']]} **{t['name']}**: {t['why_not']}" for t in doc["not_watched"]]
    if doc["notes"]:
        lines += ["", "## Notes", "", *[f"- {n}" for n in doc["notes"]]]
    return "\n".join([*lines, ""])


def summary(doc: dict) -> str:
    c = doc["counts"]
    missing = f"{c['missing']} did not run, " if c["missing"] else ""
    return (f"Since {doc['since'].replace('T', ' ')}, at {doc['at'].replace('T', ' ')}: {doc['ran']} ran: {c['ok']} fine, {c['failed']} failed, "
            f"{missing}{c['suspect']} suspect, {c['slow']} slow, {c['recovered']} recovered, {c['running']} still running.")


def _section(lines: list[str], title: str, alerts: list[dict]) -> None:
    if not alerts:
        return
    lines += [f"## {title} ({len(alerts)})", ""]
    for a in alerts:
        lines.append(f"- **[{a['env']}] {a['subject']}**: {a['message']}")
        if a.get("next"):
            lines.append(f"  - `{a['next']}`")
        if a.get("ack_note"):
            lines.append(f"  - acknowledged: {a['ack_note']}")
    lines.append("")


def _table(lines: list[str], doc: dict) -> None:
    several = len(doc["sources"]) > 1
    lines += [f"## What ran ({doc['ran']})", ""]
    if not doc["activities"]:
        lines += ["Nothing has run in this period.", ""]
        return
    head = ["State", *(["Source"] if several else []), "Name", "Runs", "Last start", "Took", "Usual", "What it runs"]
    lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for a in doc["activities"]:
        last = a["runs"][0] if a["runs"] else {}
        count = f"{len(a['runs'])}{'+' if a['more_runs'] else ''}"
        cells = [a["state"].replace("missing", "did not run"), *([a["source"]] if several else []), a["name"], count, (last.get("start") or "-").replace("T", " "),
                 _clock(last.get("duration_s")), _clock(a["usual_s"]), a["what"]]
        lines.append("| " + " | ".join(str(c).replace("|", "/") for c in cells) + " |")
    lines.append("")


def markdown(doc: dict) -> str:
    lines = [f"# {doc['verdict']}", "", summary(doc), "", looked_at(doc), ""]
    if doc.get("start_here"):
        lines += [f"**Start here: {doc['start_here']}**: {doc['start_because']}.", ""]
    blind = [f"**{s['source']}**: could not read {part}: {why}" for s in doc["sources"] for part, why in s.get("unread", {}).items()]
    for severity, title in HEADINGS.items():
        _section(lines, title, [a for a in doc["attention"] if a["severity"] == severity])
    _section(lines, "Acknowledged", doc["acknowledged"])
    if doc["resolved"]:
        lines += [f"## Resolved today ({len(doc['resolved'])})", ""]
        lines += [f"- [{a['env']}] {a['rule']}: {a['subject']}" for a in doc["resolved"]] + [""]
    drawn = gantt.text(doc["timeline"])
    if drawn:
        lines += ["## The period", "", "```", drawn, "```", ""]
    _table(lines, doc)
    if blind:
        lines += ["## Not read", "", *[f"- {b}" for b in blind], ""]
    if doc.get("not_watched"):
        lines += ["## Not watched", "", *[f"- {t['name']}: {t['why']}" for t in doc["not_watched"]], ""]
    if doc.get("notes"):
        lines += ["## Notes", "", *[f"- {n}" for n in doc["notes"]], ""]
    where = ", ".join(f"{s['source']} ({s['location'] or 'no answer'}, watching {s['watched']})" for s in doc["sources"])
    return "\n".join([*lines, f"Sources: {where}.", "", "This is the whole picture: `loadwatch status` and `alerts` show these same alerts.", ""])
