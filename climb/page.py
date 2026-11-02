#!/usr/bin/env python3
"""Hill climb on the HTML report: can someone who only looks at the page tell what needs them?

morning.py gives agents the CLI. Here a reader gets screenshots of the real page in Chrome and nothing else, as someone on a phone would:

    first    the first screen on a phone (390 x 844), before scrolling: what to fix first?
    phone    the whole page on a phone, screen by screen: what needs attention, what first?
    chart    the whole page on a laptop, or on a phone: questions the timeline answers

A screenshot cannot hover or scroll sideways, so what needs either does not count. The page's own score, 100 points per morning with no
reader and no noise, measured in Chrome on the same page: 15 the verdict, 15 what to start with and 15 why are all on the phone's first
screen; 20 no sideways scrolling of the page; 15 the share of the chart seen on a phone without scrolling it; 10 no text under 12 px on a
phone; 10 text contrast of at least 4.5, in light and in dark.

    uv run python climb/page.py evaluate --label base
    uv run python climb/page.py compare new --best base

The rule, fixed on 2026-11-02 before the first measurement, the same shape as morning.py's:
- guards, outside this script: all tests, and a look at the screenshots of whatever is kept, in light and dark, phone and laptop;
- kept when readers do better: gain of at least half a task in points per run, and z >= 1.64, paired by agent and task;
- kept when the page's own score rises and readers are no worse (z >= -1); the verdict says so, and that score was written by the same hand
  as the changes, so a keep on it alone is the weaker kind;
- reverted otherwise.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CLIMB = Path(__file__).resolve().parent
sys.path.insert(0, str(CLIMB))
import morning  # noqa: E402
import worlds  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PHONE, LAPTOP = (390, 844, 2), (1280, 900, 1)      # width, height, pixels per point
SLICES = 10
Z, NOT_WORSE = 1.64, 1.0
AGENTS = ["claude:haiku", "codex:gpt-6-luna"]
FIRST = ("root-cause", "new-vs-ongoing", "stale-pile", "person-test-run", "fabric-refresh", "blast-radius", "mixed", "two-roots", "quiet-night",
         "stuck-blocks-deadline", "big-morning", "common-cause")
WHOLE = ("new-vs-ongoing", "person-test-run", "blast-radius", "mixed", "recovered", "common-cause")
CHART = (("laptop", "q-longest"), ("laptop", "q-running"), ("laptop", "q-finished-by"), ("laptop", "q-running-at"), ("laptop", "q-usual"),
         ("phone", "q-finished-by"), ("phone", "q-running-at"))
HELD_FIRST = ("healthy-estate", "one-cause-and-another", "flaky-refresh", "deploy-was-fine", "head-did-not-start")
HELD_WHOLE = ("one-cause-and-another", "flaky-pipeline", "slowing-refresh")
HELD_CHART = (("laptop", "h-how-many"), ("laptop", "q-python"), ("laptop", "q-how-often"), ("phone", "q-why"), ("phone", "q-which-server"))

INTRO = "Below are screenshots of this morning's report on our data platform: SQL Server Agent jobs, and Fabric. {what} Read them with what you have, and go only by what you can see in them."
ASK_FIRST = INTRO.format(what="It is the first screen on a phone, before any scrolling.") + """ Which one thing should be fixed first?
End your reply with exactly this line, using the job, package, model or server name:
FIRST: name   (or FIRST: none if nothing needs fixing, or FIRST: unknown if you cannot tell from this)"""
ASK_WHOLE = INTRO.format(what="They are the whole page on a phone, from the top down.") + """ What needs attention today, and which one thing should be fixed
first? Things that fixed themselves do not need attention, and "nothing" is a fine answer if it is true.
End your reply with exactly these two lines, using the job, package, model or server names:
ATTENTION: name, name, name   (or ATTENTION: none)
FIRST: name   (or FIRST: none)"""
ASK_CHART = INTRO.format(what="They are the whole page on a {device}, from the top down.") + """ {ask}
End your reply with exactly this one line, holding only what was asked for: a yes or no, a number, or names (comma separated):
ANSWER: your answer"""

MEASURE = """<script>(function(){
var W=__WANT__,out={};function own(n){var p=n.parentElement;return p&&!p.closest('script,style,title,pre,#tip,.tip')}
function shown(e){var s=getComputedStyle(e);return s.display!='none'&&s.visibility!='hidden'}
var walk=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT),nodes=[],n;while(n=walk.nextNode()){if(n.nodeValue.trim()&&own(n)&&shown(n.parentElement))nodes.push(n)}
function box(n){var r=document.createRange();r.selectNodeContents(n);return r.getBoundingClientRect()}
function seen(text){if(!text)return true;var t=text.toLowerCase();return nodes.some(function(x){var b=box(x);return x.nodeValue.toLowerCase().indexOf(t)>=0&&b.width>0&&b.top>=0&&b.bottom<=innerHeight&&b.right<=innerWidth+1})}
function lum(c){var v=c.map(function(x){x/=255;return x<=.03928?x/12.92:Math.pow((x+.055)/1.055,2.4)});return .2126*v[0]+.7152*v[1]+.0722*v[2]}
function rgb(s){var m=s.match(/[\\d.]+/g).map(Number);return{c:m.slice(0,3),a:m.length>3?m[3]:1}}
function ground(e){for(;e;e=e.parentElement){var b=rgb(getComputedStyle(e).backgroundColor);if(b.a>.9)return b.c}return[255,255,255]}
var small=99,contrast=99;nodes.forEach(function(x){var p=x.parentElement,s=getComputedStyle(p),size=parseFloat(s.fontSize),g=p.closest('svg');
if(g&&g.viewBox&&g.viewBox.baseVal.width){size*=g.getBoundingClientRect().width/g.viewBox.baseVal.width}small=Math.min(small,size);
var fore=rgb(g?s.fill:s.color).c,a=lum(fore),b=lum(ground(g||p));contrast=Math.min(contrast,(Math.max(a,b)+.05)/(Math.min(a,b)+.05))});
var charts=[].slice.call(document.querySelectorAll('svg.gantt')).filter(function(c){return c.getBoundingClientRect().width>0});
var share=1;charts.forEach(function(c){var r=c.getBoundingClientRect();share=Math.min(share,Math.min(1,(innerWidth-Math.max(r.left,0))/r.width))});
out={width:innerWidth,verdict:seen(W.verdict),start:seen(W.start),why:seen(W.why),overflow:document.documentElement.scrollWidth>innerWidth+1,share:share,small:small,contrast:contrast,height:document.documentElement.scrollHeight};
parent.postMessage(JSON.stringify(out),'*')})()</script>"""
HOST = """<!doctype html><body style="margin:0"><iframe src="{page}" style="border:0;display:block;width:{w}px;height:{h}px"></iframe><pre id="m"></pre>
<script>addEventListener('message',function(e){{document.getElementById('m').textContent=e.data}})</script>"""


def chrome(profile: Path, *args: str) -> str:
    p = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--allow-file-access-from-files", f"--user-data-dir={profile}", *args],
                       capture_output=True, text=True, timeout=120)
    return p.stdout


def scheme(html: str, dark: bool) -> str:
    return html.replace("@media (prefers-color-scheme:dark)", "@media all" if dark else "@media not all")


def measure(d: Path, html: str, want: dict, size: tuple, dark: bool) -> dict:
    w, h, _ = size
    (d / "m.html").write_text(scheme(html, dark).replace("</html>", MEASURE.replace("__WANT__", json.dumps(want)) + "</html>"))
    (d / "mh.html").write_text(HOST.format(page="m.html", w=w, h=h))
    dom = chrome(d / "prof", f"--window-size={max(w, 500)},{h + 40}", "--virtual-time-budget=4000", "--dump-dom", f"file://{d}/mh.html")
    found = re.search(r'<pre id="m">(\{.*?\})</pre>', dom)
    if not found:
        raise RuntimeError(f"no measurement from {d}")
    return json.loads(found.group(1).replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&"))


def screens(d: Path, name: str, size: tuple, height: int, first_only: bool) -> list[Path]:
    """The page cut in screens, as many as a reader would thumb through."""
    w, h, scale = size
    tall = h if first_only else height
    (d / "host.html").write_text(HOST.format(page="light.html", w=w, h=tall))
    whole = d / f"{name}.png"
    chrome(d / "prof", f"--window-size={max(w, 500)},{tall}", f"--force-device-scale-factor={scale}", "--virtual-time-budget=4000", f"--screenshot={whole}", f"file://{d}/host.html")
    out = []
    step = h - 40
    for i in range(1 if first_only else min(SLICES, max(1, (tall - 40) // step + 1))):
        part = d / f"{name}-{i + 1:02d}.png"
        top = i * step
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", whole, "-vf", f"crop={w * scale}:{min(h, tall - top) * scale}:0:{top * scale}", "-frames:v", "1", part], check=True)
        out.append(part)
    whole.unlink()
    return out


def why_text(doc: dict) -> str:
    """Words of the reason for the first thing, away from any timestamp a page may reword."""
    a = next((x for x in doc["attention"] if x["subject"] == doc.get("start_here")), None)
    if not a:
        return ""
    parts = re.split(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d|\bat\b", a["message"])
    return max(parts, key=len).strip(" .:;,")[:28]


def prepare(s: worlds.Scenario, root: Path, bin_dir: Path) -> dict:
    """One morning's page: its screenshots and its own score."""
    world = morning.prepare(s, root / "world", bin_dir)
    env = morning.run_env(world, bin_dir)
    page = root / "page.html"
    subprocess.run([bin_dir / "loadwatch", "report", "--format", "html", "--out", page], cwd=world["work"], env=env, capture_output=True)
    doc = json.loads(subprocess.run([bin_dir / "loadwatch", "report", "--format", "json"], cwd=world["work"], env=env, capture_output=True, text=True).stdout)
    html = page.read_text(encoding="utf-8")
    want = {"verdict": doc["verdict"], "start": doc.get("start_here") or "", "why": why_text(doc)}
    light, dark = measure(root, html, want, PHONE, False), measure(root, html, want, PHONE, True)
    (root / "light.html").write_text(scheme(html, False), encoding="utf-8")
    laptop = measure(root, html, want, LAPTOP, False)
    contrast = min(light["contrast"], dark["contrast"])
    parts = {"verdict": 15 * light["verdict"], "start": 15 * light["start"], "why": 15 * light["why"], "no_overflow": 20 * (not light["overflow"] and not laptop["overflow"]),
             "chart": round(15 * light["share"], 1), "text": 10 * (light["small"] >= 12), "contrast": 10 * (contrast >= 4.5)}
    return {"points": round(sum(parts.values()), 1), "parts": parts, "contrast": round(contrast, 2), "small": round(light["small"], 1), "share": round(light["share"], 2),
            "height": light["height"], "bytes": len(html.encode()), "shots": {
                "first": [str(p) for p in screens(root, "first", PHONE, light["height"], True)],
                "phone": [str(p) for p in screens(root, "phone", PHONE, light["height"], False)],
                "laptop": [str(p) for p in screens(root, "laptop", LAPTOP, laptop["height"], False)]}}


def tasks(held: bool) -> list[tuple]:
    every = {s.name: s for s in [*worlds.SCENARIOS, *worlds.HELD_OUT]}
    first, whole, chart = (HELD_FIRST, HELD_WHOLE, HELD_CHART) if held else (FIRST, WHOLE, CHART)
    return [("first", every[n]) for n in first] + [("phone", every[n]) for n in whole] + [(view, every[n]) for view, n in chart]


def read(view: str, s: worlds.Scenario, shots: list[str], agent: str, i: int, out: Path, budget: float) -> dict:
    d = out / agent.replace(":", "-") / f"{view}-{s.name}-{i}"
    d.mkdir(parents=True, exist_ok=True)
    for p in shots:
        shutil.copy(p, d)
    names = [Path(p).name for p in shots]
    prompt = (ASK_CHART.format(device=view, ask=s.ask) if s.ask else ASK_FIRST if view == "first" else ASK_WHOLE)
    backend, _, model = agent.partition(":")
    if backend == "claude":
        prompt += "\n\nThe screenshots, in order, are in this folder: " + ", ".join(names)
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", model, "--setting-sources", "project",
               "--max-budget-usd", str(budget), "--allowedTools", "Read"]
    else:
        cmd = ["codex", "exec", "-m", model, "--json", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", prompt]
        for n in names:
            cmd += ["-i", n]
    env = {k: v for k, v in morning.os.environ.items() if k not in ("CLAUDECODE", "VIRTUAL_ENV")}
    transcript = d / "transcript.jsonl"
    started = time.time()
    try:
        with open(transcript, "w") as f:
            subprocess.run(cmd, cwd=d, env=env, stdout=f, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=300)
    except subprocess.TimeoutExpired:
        pass
    _, final, cost, tokens = morning.steps_of(transcript, backend)
    r = grade(view, s, final, out)
    r = {"scenario": f"{view}:{s.name}", "agent": agent, **r, "score": r["points"], "cost_usd": cost, "tokens": tokens, "seconds": round(time.time() - started), "final": final[-500:]}
    (d / "grade.json").write_text(json.dumps(r, indent=1))
    for n in names:
        (d / n).unlink()
    return r


def grade(view: str, s: worlds.Scenario, final: str, out: Path) -> dict:
    world = out / "pages" / s.name / "world"
    if view == "first":
        lines = [re.sub(r"[*`]", "", x).strip() for x in final.splitlines() if re.match(r"(?i)^\W*first\s*:", x)]
        said = lines[-1].split(":", 1)[1].strip() if lines else "no FIRST line"
        none = said.lower() == "none"
        right = none if s.first is None else not none and any(morning.same(said, x) for x in (s.first, *s.also))
        return {"set": right, "first": right, "points": 100 if right else 0, "note": f"said {said[:80]}"}
    return morning.grade(s, final, world)


def evaluate(label: str, commit: str | None, agents: list[str], repeats: int, jobs: int, budget: float, held: bool, only_page: bool) -> dict:
    if commit is None:
        morning.guard()
    commit = commit or subprocess.run(["git", "rev-parse", "HEAD"], cwd=morning.REPO, capture_output=True, text=True).stdout.strip()
    out = CLIMB / "runs" / label
    out.mkdir(parents=True, exist_ok=True)
    bin_dir = morning.install(commit, out)
    work = tasks(held)
    mornings = {s.name: s for _, s in work}
    with ThreadPoolExecutor(min(jobs, 4)) as pool:
        pages = dict(zip(mornings, pool.map(lambda s: prepare(s, out / "pages" / s.name, bin_dir), mornings.values())))
    results = []
    if not only_page:
        runs = [(v, s, pages[s.name]["shots"][v if v in ("first", "phone") else "laptop"], a, i) for a in agents for v, s in work for i in range(repeats)]
        with ThreadPoolExecutor(jobs) as pool:
            results = list(pool.map(lambda x: read(*x, out, budget), runs))
    doc = {"label": label, "commit": commit, "pages": pages, "results": results}
    (out / "card.json").write_text(json.dumps(doc, indent=1))
    return doc


def page_score(doc: dict) -> float:
    return round(sum(p["points"] for p in doc["pages"].values()) / len(doc["pages"]), 1)


def verdict(new: dict, best: dict) -> dict:
    stats = morning.paired(new, best)
    before, after = page_score(best), page_score(new)
    half_a_task = 50 / max(1, len({r["scenario"] for r in new["results"]}))
    if stats["gain"] >= half_a_task and stats["z"] >= Z:
        why = "readers did better"
    elif after > before and stats["z"] >= -NOT_WORSE:
        why = "the page's own score rose and readers are no worse: the weaker kind of keep"
    else:
        return {"keep": False, "why": "not enough better", **stats, "page": [before, after]}
    return {"keep": True, "why": why, **stats, "page": [before, after]}


def table(doc: dict) -> str:
    rs = doc["results"]
    lines = [f"## {doc['label']} ({doc['commit'][:8]}): page {page_score(doc)} / 100, readers {sum(r['points'] >= 100 for r in rs)}/{len(rs)} right", ""]
    parts: dict = {}
    for p in doc["pages"].values():
        for k, v in p["parts"].items():
            parts[k] = parts.get(k, 0) + v / len(doc["pages"])
    lines.append("page parts: " + ", ".join(f"{k} {v:.1f}" for k, v in parts.items()))
    any_page = next(iter(doc["pages"].values()))
    lines += [f"e.g. contrast {any_page['contrast']}, smallest {any_page['small']}, chart share {any_page['share']}, phone height {any_page['height']}, {any_page['bytes']} bytes", ""]
    for view in ("first", "phone", "laptop"):
        got = [r for r in rs if r["scenario"].split(":")[0] == view or (view == "laptop" and r["scenario"].startswith("laptop"))]
        if got:
            lines.append(f"{view}: {sum(r['points'] >= 100 for r in got)}/{len(got)}")
    seen = list(dict.fromkeys(r["scenario"] for r in rs))
    for t in seen:
        got = [r for r in rs if r["scenario"] == t]
        right = sum(r["points"] >= 100 for r in got)
        if right < len(got):
            miss = next(r for r in got if r["points"] < 100)
            lines.append(f"  {t}: {right}/{len(got)}  {miss['agent'].split(':')[1]} {miss['note'][:90]}")
    lines.append(f"cost ${sum(r['cost_usd'] for r in rs):.2f}, tokens {sum(r['tokens'] for r in rs)}")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("evaluate")
    e.add_argument("--label", required=True)
    e.add_argument("--commit")
    e.add_argument("--agents", default=",".join(AGENTS))
    e.add_argument("--repeats", type=int, default=2)
    e.add_argument("--jobs", type=int, default=10)
    e.add_argument("--budget", type=float, default=0.5)
    e.add_argument("--held-out", action="store_true")
    e.add_argument("--page-only", action="store_true", help="only the page's own score: no readers")
    c = sub.add_parser("compare")
    c.add_argument("new")
    c.add_argument("--best", required=True)
    a = p.parse_args()
    if a.cmd == "evaluate":
        print(table(evaluate(a.label, a.commit, a.agents.split(","), a.repeats, a.jobs, a.budget, a.held_out, a.page_only)))
    else:
        load = lambda n: json.loads((CLIMB / "runs" / n / "card.json").read_text())   # noqa: E731
        print(json.dumps(verdict(load(a.new), load(a.best)), indent=1))


if __name__ == "__main__":
    main()
