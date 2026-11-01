"""The period as a timeline: one row per job, a bar per run, on one time axis. timeline() is plain data, for the JSON
report and for the two drawings: text() for a terminal, and svg() in the HTML report (html_report.py).

What a plain Gantt would not show, and this does: a job that should have run and did not (a labelled row with no bar),
each run against its usual duration, deadlines, and how many jobs ran at once."""
from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

SLOW_FACTOR, SLOW_SECONDS = 1.5, 60    # as ssisbliss marks a step slower
STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 180, 240, 360, 720, 1440)   # minutes between ticks
LABELS = {"ok": "Succeeded", "slow": "Slow", "suspect": "Suspect", "failed": "Failed", "canceled": "Canceled", "running": "Running"}
ICONS = {"failed": "×", "canceled": "×", "suspect": "?", "slow": "!"}


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text[:19])


def clock(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s" if s < 3600 else f"{s // 3600}h{s % 3600 // 60:02d}m"


def _status(run: dict, activity: dict, newest: bool) -> str:
    status, took, usual = run["status"], run.get("duration_s"), activity.get("usual_s")
    if status == "succeeded":
        if newest and activity["state"] == "suspect":
            return "suspect"
        return "slow" if usual and took and took >= SLOW_FACTOR * usual and took - usual >= SLOW_SECONDS else "ok"
    return status if status in LABELS else "failed"


def _ticks(start: datetime, end: datetime) -> list[datetime]:
    span = (end - start).total_seconds() / 60
    step = next((s for s in STEPS if span / s <= 10), STEPS[-1])
    first = start.replace(minute=0, second=0, microsecond=0)
    while first + timedelta(minutes=step) <= start:
        first += timedelta(minutes=step)
    out = []
    while first <= end:
        if first >= start:
            out.append(first)
        first += timedelta(minutes=step)
    return out


def _concurrency(bars: list[tuple[datetime, datetime]]) -> tuple[list[tuple[str, int]], int, str | None]:
    """How many ran at once: a step series, the peak and when it first came."""
    events = sorted(e for start, end in bars for e in ((start, 1), (max(end, start + timedelta(seconds=1)), -1)))
    steps, n, peak, peak_at = [], 0, 0, None
    for at, change in events:
        n += change
        steps.append((at.isoformat()[:19], n))
        if n > peak:
            peak, peak_at = n, at.isoformat()[:19]
    return steps, peak, peak_at


def timeline(activities: list[dict], now: datetime) -> dict:
    """Rows in the order the period ran, first start first; jobs that did not run at the bottom."""
    several = len({a["source"] for a in activities}) > 1
    rows, spans = [], []
    for a in activities:
        bars = []
        for i, run in enumerate(a["runs"]):
            start = _at(run["start"])
            end = now if run["status"] == "running" else start + timedelta(seconds=run.get("duration_s") or 0)
            bars.append({"start": start.isoformat()[:19], "end": end.isoformat()[:19], "status": _status(run, a, i == 0),
                         "took_s": (end - start).total_seconds()})
            spans.append((start, end))
        rows.append({"name": f"{a['source']} / {a['name']}" if several else a["name"], "state": a["state"], "what": a["what"],
                     "usual_s": a.get("usual_s"), "due": a.get("due"), "bars": sorted(bars, key=lambda b: b["start"])})
        if a.get("due"):
            rows[-1]["due_missed"] = _at(a["due"]) <= now and not any(
                b["status"] in ("ok", "slow", "suspect") and b["end"] <= a["due"] for b in bars)
    rows.sort(key=lambda r: (not r["bars"], r["bars"][0]["start"] if r["bars"] else "", r["name"].lower()))
    moments = [s for s, _ in spans] + [e for _, e in spans] + [_at(r["due"]) for r in rows if r["due"]] + [now]
    start, end = min(moments), max(moments)
    margin = max((end - start) * 0.02, timedelta(minutes=1))
    start, end = start - margin, end + margin
    steps, peak, peak_at = _concurrency(spans)
    return {"start": start.isoformat()[:19], "end": end.isoformat()[:19], "now": now.isoformat()[:19], "rows": rows,
            "ticks": [t.isoformat()[:19] for t in _ticks(start, end)], "running": steps, "peak": peak, "peak_at": peak_at}


# ------------------------------------------------------------------------------------------ for a terminal --

CELL = {"ok": "#", "slow": "S", "suspect": "?", "failed": "X", "canceled": "C", "running": ">"}
RANK = ("ok", "slow", "suspect", "canceled", "running", "failed")     # what a cell shows when several runs share it


def text(t: dict, width: int = 64, label: int = 26) -> str:
    """A plain-ASCII timeline: Windows consoles and redirected output do not all take block characters."""
    if not t["rows"]:
        return ""
    start, end = _at(t["start"]), _at(t["end"])
    span = (end - start).total_seconds() or 1

    def col(moment: datetime) -> int:
        return min(width - 1, max(0, int((moment - start).total_seconds() / span * width)))

    axis = [" "] * (width + 6)
    last = -6
    for tick in map(_at, t["ticks"]):
        at = col(tick)
        if at - last >= 6:
            axis[at:at + 5] = tick.strftime("%H:%M")
            last = at
    lines = [" " * (label + 1) + "".join(axis).rstrip()]
    now = col(_at(t["now"]))
    for r in t["rows"]:
        cells = ["."] * width
        cells[now] = "|"
        if r["due"]:
            cells[col(_at(r["due"]))] = "D"
        for b in r["bars"]:
            for i in range(col(_at(b["start"])), col(_at(b["end"])) + 1):
                if cells[i] not in CELL.values() or RANK.index(b["status"]) >= RANK.index(next(k for k, v in CELL.items() if v == cells[i])):
                    cells[i] = CELL[b["status"]]
        note = "  did not run" if not r["bars"] else ""
        lines.append(f"{r['name'][:label]:<{label}} {''.join(cells)}{note}")
    lines.append(f"{'':<{label}} # ok  S slow  ? suspect  X failed  C canceled  > running  D deadline  | now")
    if t["peak"] > 1:
        lines.append(f"{'':<{label}} Most at once: {t['peak']} at {t['peak_at'][11:16]}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------- for a page --

LEFT, RIGHT, TOP, ROW, BAR, STRIP = 250, 56, 28, 24, 14, 56
WIDTH = 1120


def _bar_tip(row: dict, b: dict) -> str:
    start, end = b["start"][11:19], b["end"][11:19]
    usual = f", usually {clock(row['usual_s'])}" if row.get("usual_s") else ""
    return f"{row['name']}|{LABELS[b['status']]}: {start} to {end}|took {clock(b['took_s'])}{usual}|{row['what']}"


def svg(t: dict) -> str:
    """The chart. Every string that came from a server is escaped."""
    start, end = _at(t["start"]), _at(t["end"])
    span = (end - start).total_seconds() or 1
    plot = WIDTH - LEFT - RIGHT
    rows = t["rows"]
    bottom = TOP + ROW * len(rows)
    strip = t["peak"] > 1
    height = bottom + (STRIP + 26 if strip else 10)

    def x(moment: datetime | str) -> float:
        at = _at(moment) if isinstance(moment, str) else moment
        return round(LEFT + (at - start).total_seconds() / span * plot, 1)

    out = [f'<svg class="gantt" viewBox="0 0 {WIDTH} {height}" role="img" aria-label="Timeline of {len(rows)} jobs. '
           f'Every run is also in the table below." xmlns="http://www.w3.org/2000/svg">']
    for tick in t["ticks"]:
        at = x(tick)
        out.append(f'<line class="grid" x1="{at}" x2="{at}" y1="{TOP - 6}" y2="{height - 18 if strip else bottom}"/>')
        label = tick[11:16] if tick[11:16] != "00:00" else f"{tick[8:10]}/{tick[5:7]}"
        if abs(at - x(t["now"])) >= 34:      # the "now" label sits on the same line
            out.append(f'<text class="tick" x="{at}" y="{TOP - 12}" text-anchor="middle">{label}</text>')
    for i, r in enumerate(rows):
        top = TOP + ROW * i
        mid = top + ROW / 2
        name = escape(r["name"])
        if i % 2:
            out.append(f'<rect class="band" x="0" y="{top}" width="{WIDTH}" height="{ROW}"/>')
        short = name if len(r["name"]) <= 34 else escape(r["name"][:33]) + "…"
        out.append(f'<text class="name" x="{LEFT - 12}" y="{mid + 4}" text-anchor="end">{short}<title>{name}</title></text>')
        if not r["bars"]:
            out.append(f'<text class="absent" x="{LEFT + 8}" y="{mid + 4}">! did not run</text>')
        for n, b in enumerate(r["bars"]):
            left, right = x(b["start"]), x(b["end"])
            width = max(3.0, round(right - left, 1))
            crowded = n + 1 < len(r["bars"]) and x(r["bars"][n + 1]["start"]) - (left + width) < 14    # one icon per cluster of runs
            out.append(f'<rect class="bar {b["status"]}" x="{left}" y="{mid - BAR / 2}" width="{width}" height="{BAR}" rx="3"/>')
            usual = r.get("usual_s")
            tip = x(_at(b["start"]) + timedelta(seconds=usual)) if usual else None
            if tip is not None and tip - left >= 6 and tip <= left + width + 40:
                out.append(f'<line class="usual" x1="{tip}" x2="{tip}" y1="{mid - BAR / 2 - 2}" y2="{mid + BAR / 2 + 2}"/>')
            if b["status"] in ICONS and not crowded:
                out.append(f'<text class="icon" x="{left + width + 4}" y="{mid + 4}">{ICONS[b["status"]]}</text>')
            hit = max(width, 12.0)
            out.append(f'<rect class="hit" x="{left + width / 2 - hit / 2}" y="{top}" width="{hit}" height="{ROW}" tabindex="0" '
                       f'data-tip="{escape(_bar_tip(r, b), quote=True)}"><title>{escape(_bar_tip(r, b).replace("|", ". "))}</title></rect>')
        if r["due"]:
            at = x(r["due"])
            missed = " missed" if r.get("due_missed") else ""
            said = "missed the deadline of" if missed else "due by"
            out.append(f'<path class="due{missed}" d="M{at} {mid - 7}l6 7-6 7-6-7z"><title>{name}: {said} {r["due"][11:16]}</title></path>')
    now = x(t["now"])
    out.append(f'<line class="now" x1="{now}" x2="{now}" y1="{TOP - 6}" y2="{height - 18 if strip else bottom}"/>')
    out.append(f'<text class="nowlabel" x="{now}" y="{TOP - 12}" text-anchor="middle">now</text>')
    if strip:
        base, top = height - 18, bottom + 22
        scale = (base - top) / t["peak"]
        path = [f"M{LEFT} {base}"]
        level = base
        for moment, n in t["running"]:
            at = x(moment)
            path.append(f"L{at} {level}")
            level = round(base - n * scale, 1)
            path.append(f"L{at} {level}")
        out.append(f'<path class="strip" d="{" ".join(path)} L{x(t["end"])} {level} L{x(t["end"])} {base}z"/>')
        out.append(f'<line class="axis" x1="{LEFT}" x2="{WIDTH - RIGHT}" y1="{base}" y2="{base}"/>')
        out.append(f'<text class="name" x="{LEFT - 12}" y="{top + 12}" text-anchor="end">Jobs running at once</text>')
        out.append(f'<text class="tick" x="{LEFT - 12}" y="{top + 27}" text-anchor="end">most: {t["peak"]} at {t["peak_at"][11:16]}</text>')
    out.append("</svg>")
    return "".join(out)
