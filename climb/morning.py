#!/usr/bin/env python3
"""Hill climb on what loadwatch is for: does whoever reads it in the morning know what needs them?

Small agents get a fake morning and the CLI, nothing else, and must name what needs attention and what to fix first. The
tool's own JSON report is graded on the same answers, which has no noise. A change must help on those, not just tidy.

    uv run python climb/morning.py evaluate --label base
    uv run python climb/morning.py compare new --best base

An agent's run scores 100 for the right set and the right first thing, 60 for the right set, 30 for the right first thing,
minus one point per tool call (at most 30). Saying nothing needs attention when something does scores 0, and reading the fake
servers' files instead of using the tools scores 0. The report's own score, per scenario: 40 finds everything, 30 raises no
false alarm, 30 puts the right thing first.

The rule, fixed before the first measurement:
- guards, outside this script: all tests, and `report` on 200 jobs no slower by 25% and 0.15 s;
- kept when agents do better: gain >= MIN_GAIN points per run and z >= Z, paired by agent and scenario;
- kept when the report's own score rises and agents are no worse (z >= -NOT_WORSE); the verdict says so;
- reverted otherwise.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CLIMB = Path(__file__).resolve().parent
REPO = CLIMB.parent
sys.path.insert(0, str(CLIMB))
import worlds  # noqa: E402

AGENTS = ["claude:haiku", "codex:gpt-6-luna"]
MIN_GAIN, Z, NOT_WORSE = 5.0, 1.64, 1.0
STEP_CAP = 30
SSISBLISS = REPO.parent / "Onpremtools"
PROMPT = """It's the morning and you're on call for our data platform: SQL Server Agent jobs, and Fabric. The `loadwatch` command is
installed; start with `loadwatch --help`. Tell me what needs attention today, and which one thing to fix first. Do not change
anything. Things that fixed themselves do not need attention, and "nothing" is a fine answer if it is true.
End your reply with exactly these two lines, using the job, package, model or server names:
ATTENTION: name, name, name   (or ATTENTION: none)
FIRST: name   (or FIRST: none)"""
TOOLS = ["Bash(loadwatch:*)", "Bash(ssisbliss:*)"]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower().replace(".dtsx", "")).strip()


def same(said: str, real: str) -> bool:
    a, b = _norm(said), _norm(real)
    return bool(a) and (b in a or a in b)


def answer(text: str) -> tuple[list[str], str | None] | None:
    lines = [re.sub(r"[*`]", "", x).strip() for x in text.splitlines()]
    att = [x for x in lines if re.match(r"(?i)^attention\s*:", x)]
    first = [x for x in lines if re.match(r"(?i)^first\s*:", x)]
    if not att or not first:
        return None
    names = [n.strip() for n in re.split(r"[,;]", att[-1].split(":", 1)[1]) if n.strip() and n.strip().lower() != "none"]
    one = first[-1].split(":", 1)[1].strip()
    return names, None if one.lower() in ("none", "") else one


def grade(s: worlds.Scenario, text: str) -> dict:
    got = answer(text)
    if got is None:
        return {"set": False, "first": False, "points": 0, "note": "no ATTENTION and FIRST lines"}
    names, first = got
    found = all(any(same(n, real) for n in names) for real in s.attention)
    extra = [n for n in names if not any(same(n, real) for real in s.attention)]
    right_set = found and not extra
    right_first = (first is None) if s.first is None else (first is not None and same(first, s.first))
    points = 100 if right_set and right_first else 60 if right_set else 30 if right_first else 0
    if s.attention and not names:      # a false all clear
        points = 0
    return {"set": right_set, "first": right_first, "points": points, "note": f"said {names}, first {first}"}


def run_env(world: dict, bin_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "VIRTUAL_ENV")}
    return {**env, **world["env"], "PATH": f"{bin_dir}:{env['PATH']}", "AZURE_CONFIG_DIR": str(world["root"] / "noaz")}


def prepare(s: worlds.Scenario, root: Path, bin_dir: Path) -> dict:
    world = worlds.build(s, root)
    subprocess.run([bin_dir / "loadwatch", "discover"], cwd=world["work"], env=run_env(world, bin_dir), capture_output=True)
    worlds.stop_answering(world, s)
    return world


def report_score(s: worlds.Scenario, root: Path, bin_dir: Path) -> dict:
    """The tool alone: no reader, no noise."""
    world = prepare(s, root, bin_dir)
    p = subprocess.run([bin_dir / "loadwatch", "report", "--format", "json"], cwd=world["work"], env=run_env(world, bin_dir), capture_output=True, text=True)
    doc = json.loads(p.stdout)
    items = doc.get("attention", [])
    names = [a["subject"] for a in items]
    found = all(any(same(n, real) for n in names) for real in s.attention)
    false = [n for n in names if not any(same(n, real) for real in s.attention)]
    top = doc.get("start_here") or (names[0] if names else None)
    first = (top is None) if s.first is None else (top is not None and same(str(top), s.first))
    return {"points": 40 * found + 30 * (not false) + 30 * first, "found": found, "false": false, "first": top}


def steps_of(path: Path, backend: str) -> tuple[int, str, float, int]:
    steps, final, cost, tokens = 0, "", 0.0, 0
    for line in path.read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if backend == "claude":
            if ev.get("type") == "assistant":
                steps += sum(p.get("type") == "tool_use" for p in ev["message"].get("content", []))
            elif ev.get("type") == "result":
                final, cost = ev.get("result") or "", ev.get("total_cost_usd") or 0.0
                u = ev.get("usage") or {}
                tokens = sum(u.get(k) or 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        else:
            item = ev.get("item") or {}
            if ev.get("type") == "item.completed" and item.get("type") == "command_execution":
                steps += 1
            elif ev.get("type") == "item.completed" and item.get("type") == "agent_message":
                final = item.get("text", "")
            elif ev.get("type") == "turn.completed":
                tokens = sum(v for v in (ev.get("usage") or {}).values() if isinstance(v, int))
    return steps, final, cost, tokens


def run_one(s: worlds.Scenario, agent: str, i: int, out: Path, bin_dir: Path, budget: float) -> dict:
    d = out / agent.replace(":", "-") / f"{s.name}-{i}"
    world = prepare(s, d / "world", bin_dir)
    backend, _, model = agent.partition(":")
    if backend == "claude":
        cmd = ["claude", "-p", PROMPT, "--output-format", "stream-json", "--verbose", "--model", model, "--setting-sources", "project",
               "--max-budget-usd", str(budget), "--allowedTools", *TOOLS]
    else:
        cmd = ["codex", "exec", "-m", model, "--json", "--ephemeral", "--skip-git-repo-check", "-s", "workspace-write",
               "--add-dir", str(d / "world"), "-c", 'shell_environment_policy.inherit="all"', PROMPT]
    started = time.time()
    transcript = d / "transcript.jsonl"
    try:
        with open(transcript, "w") as f:
            subprocess.run(cmd, cwd=world["work"], env=run_env(world, bin_dir), stdout=f, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=420)
    except subprocess.TimeoutExpired:
        pass
    steps, final, cost, tokens = steps_of(transcript, backend)
    g = grade(s, final)
    peeked = "msdb.json" in transcript.read_text() or "state.json" in transcript.read_text() or "fabric.json" in transcript.read_text()
    points = 0 if peeked else g["points"] - min(STEP_CAP, steps) if g["points"] else 0
    r = {"scenario": s.name, "agent": agent, **g, "score": points, "steps": steps, "cost_usd": cost, "tokens": tokens, "peeked": peeked,
         "seconds": round(time.time() - started), "final": final[-900:]}
    (d / "grade.json").write_text(json.dumps(r, indent=1))
    return r


def install(commit: str, out: Path) -> Path:
    """The tool at this commit, in its own environment, so that the working tree can move on during a measurement."""
    tree = out / "tool"
    if tree.exists():
        shutil.rmtree(tree)
    subprocess.run(["git", "clone", "-q", str(REPO), str(tree)], check=True)
    subprocess.run(["git", "checkout", "-q", commit], cwd=tree, check=True)
    subprocess.run(["uv", "venv", "-q", "--python", "3.12", str(tree / ".venv")], check=True)
    subprocess.run(["uv", "pip", "install", "-q", "--python", str(tree / ".venv/bin/python"), "--no-deps", "-e", str(tree), str(SSISBLISS), "sqlglot>=30,<31", "pyodbc"],
                   check=True)
    return tree / ".venv/bin"


def evaluate(label: str, commit: str | None, agents: list[str], repeats: int, jobs: int, budget: float, held_out: bool, only_report: bool) -> dict:
    commit = commit or subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    out = CLIMB / "runs" / label
    out.mkdir(parents=True, exist_ok=True)
    bin_dir = install(commit, out)
    scenarios = worlds.HELD_OUT if held_out else worlds.SCENARIOS
    report = {s.name: report_score(s, out / "report" / s.name, bin_dir) for s in scenarios}
    results = []
    if not only_report:
        work = [(s, a, i) for a in agents for s in scenarios for i in range(repeats)]
        with ThreadPoolExecutor(jobs) as pool:
            results = list(pool.map(lambda x: run_one(*x, out, bin_dir, budget), work))
    doc = {"label": label, "commit": commit, "report": report, "results": results}
    (out / "card.json").write_text(json.dumps(doc, indent=1))
    return doc


def paired(new: dict, best: dict) -> dict:
    def means(doc):
        pairs: dict = {}
        for r in doc["results"]:
            pairs.setdefault((r["agent"], r["scenario"]), []).append(r["score"])
        return {k: sum(v) / len(v) for k, v in pairs.items()}
    a, b = means(new), means(best)
    diffs = [a[k] - b[k] for k in a if k in b]
    n = len(diffs)
    gain = sum(diffs) / n if n else 0.0
    sd = (sum((d - gain) ** 2 for d in diffs) / (n - 1)) ** 0.5 if n > 1 else 0.0
    se = sd / n ** 0.5 if n else 0.0
    z = gain / se if se else (0.0 if gain == 0 else math.copysign(math.inf, gain))
    return {"gain": round(gain, 2), "se": round(se, 2), "z": round(z, 2), "pairs": n}


def verdict(new: dict, best: dict) -> dict:
    stats = paired(new, best)
    before, after = (sum(r["points"] for r in d["report"].values()) for d in (best, new))
    if stats["gain"] >= MIN_GAIN and stats["z"] >= Z:
        why = "agents did better"
    elif after > before and stats["z"] >= -NOT_WORSE:
        why = "the report is better and agents are no worse"
    else:
        return {"keep": False, "why": "not enough better", **stats, "report": [before, after]}
    return {"keep": True, "why": why, **stats, "report": [before, after]}


def table(doc: dict) -> str:
    rs = doc["results"]
    scenarios = list(doc["report"])
    lines = [f"## {doc['label']} ({doc['commit'][:8]})", "", f"report score {sum(r['points'] for r in doc['report'].values())} / {100 * len(doc['report'])}", ""]
    lines += ["| scenario | report | first in report | " + " | ".join(a.split(":")[1] for a in AGENTS) + " |", "|---|---|---|" + "---|" * len(AGENTS)]
    for s in scenarios:
        cells = []
        for a in AGENTS:
            mine = [r for r in rs if r["scenario"] == s and r["agent"] == a]
            cells.append(f"{sum(r['set'] for r in mine)}s {sum(r['first'] for r in mine)}f /{len(mine)} · {sum(r['steps'] for r in mine) // max(1, len(mine))}st · {sum(r['score'] for r in mine) // max(1, len(mine))}")
        rep = doc["report"][s]
        lines.append(f"| {s} | {rep['points']} | {str(rep['first'])[:28]} | " + " | ".join(cells) + " |")
    total = {a: [r for r in rs if r["agent"] == a] for a in AGENTS}
    for a, mine in total.items():
        lines.append(f"\n{a}: score {sum(r['score'] for r in mine)}, right {sum(r['set'] and r['first'] for r in mine)}/{len(mine)}, steps {sum(r['steps'] for r in mine)}, "
                     f"tokens {sum(r['tokens'] for r in mine)}, ${sum(r['cost_usd'] for r in mine):.2f}, peeked {sum(r['peeked'] for r in mine)}")
    return "\n".join(lines) + "\n"


def load(label: str) -> dict:
    return json.loads((CLIMB / "runs" / label / "card.json").read_text())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("evaluate")
    e.add_argument("--label", required=True)
    e.add_argument("--commit")
    e.add_argument("--agents", default=",".join(AGENTS))
    e.add_argument("--repeats", type=int, default=2)
    e.add_argument("--jobs", type=int, default=6)
    e.add_argument("--budget", type=float, default=0.75)
    e.add_argument("--held-out", action="store_true")
    e.add_argument("--report-only", action="store_true", help="only the tool's own score: no agents")
    c = sub.add_parser("compare")
    c.add_argument("new")
    c.add_argument("--best", required=True)
    a = p.parse_args()
    if a.cmd == "evaluate":
        print(table(evaluate(a.label, a.commit, a.agents.split(","), a.repeats, a.jobs, a.budget, a.held_out, a.report_only)))
    else:
        new, best = load(a.new), load(a.best)
        print(table(best), table(new), json.dumps(verdict(new, best), indent=1), sep="\n")


if __name__ == "__main__":
    main()
