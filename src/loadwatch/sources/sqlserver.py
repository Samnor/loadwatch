"""A SQL Server: its SQL Agent jobs, through ssisbliss. Windows integrated auth is ssisbliss's default: an
environment without sql_user logs in as whoever runs loadwatch."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from statistics import median

from ..activity import CANCELED, FAILED, RUNNING, SUCCEEDED, Activity, Result, Run
from ..config import Config
from ..observe import Observation, observe
from ..rules import _at, evaluate, last_deadline

_PYTHON = re.compile(r"\bpython[\d.]*(\.exe)?\b|\bpy(\.exe)?\s|\.py\b", re.I)
ABSENT = ("deadline-missed", "overdue", "missed-schedule")    # should have run: a row with no bar says so
SILENT = ("step-failed-silently", "script-output-error")
_LABEL = {"PowerShell": "PowerShell", "TSQL": "T-SQL", "CmdExec": "command", "SSIS": "SSIS"}


def describe(view: dict) -> str:
    """What a job runs, in words: its packages and scripts."""
    out = []
    for s in view.get("steps", []):
        ssis = s.get("ssis") or {}
        if s["subsystem"] == "SSIS":
            name = "/".join(x for x in (ssis.get("project"), ssis.get("package") or ssis.get("file")) if x)
            out.append(f"SSIS {name}".strip())
        elif s["subsystem"] == "CmdExec" and _PYTHON.search(s.get("command") or ""):
            out.append("Python")
        else:
            out.append(_LABEL.get(s["subsystem"], s["subsystem"]))
    unique = list(dict.fromkeys(out))
    return ", ".join(unique[:3]) + (f" +{len(unique) - 3} more" if len(unique) > 3 else "")


def _ended(run: dict) -> datetime:
    return _at(run["start"]) + timedelta(seconds=run.get("duration_s") or 0)


def activities(o: Observation, since: datetime) -> list[Activity]:
    """The jobs that ran in the period: a run counts if it ended in it, so a load that crossed midnight is there."""
    out = []
    for v in o.jobs:
        history = [r for r in o.histories.get(v["name"], []) if r.get("start")]
        runs = [r for r in history if _ended(r) >= since]
        if not runs and not v["running"]:
            continue
        shown = [Run(v["running_since"][:19], RUNNING)] if v["running"] and v.get("running_since") else []
        shown += [Run(r["start"], r.get("outcome") or "unknown", r.get("duration_s")) for r in runs]
        usual = [r["duration_s"] for r in history if r.get("outcome") == SUCCEEDED and r.get("duration_s") is not None]
        bad = [r for r in runs if r.get("outcome") in (FAILED, CANCELED)]
        state = (RUNNING if v["running"] else FAILED if runs and runs[0].get("outcome") in (FAILED, CANCELED)
                 else "recovered" if bad else "ok")
        out.append(Activity(o.env, "sql_agent_job", v["name"], describe(v), state, shown, median(usual) if usual else None,
                            more_runs=len(history) == len(runs) and len(history) >= o.history_runs))
    return sorted(out, key=lambda a: a.runs[0].start if a.runs else "")


class SqlServerSource:
    kind = "sql_server"

    def __init__(self, name: str, target: list[str], cfg: Config):
        """target: how ssisbliss reaches it, --env NAME or --server NAME."""
        self.name, self.target, self.cfg = name, target, cfg

    def collect(self, since: datetime, now: datetime, memory: dict[str, dict], thorough: bool) -> Result:
        hours = max(self.cfg.window_hours, (now - since).total_seconds() / 3600)
        seen = {job: m.get("last_start") for job, m in memory.items()}
        o = observe(self.name, self.target, hours, seen, thorough)
        signals, looked, remembered = evaluate(o, self.cfg, memory, now)
        found = activities(o, since)
        doubted = {s.subject.split(" / ")[0] for s in signals if s.rule in SILENT}
        for a in found:
            if a.state == "ok" and a.name in doubted:
                a.state = "suspect"      # the job says it succeeded, and something in its run says otherwise
            elif a.state == "ok" and any(s.rule == "ran-long" and s.subject == a.name for s in signals):
                a.state = "slow"
        ran = {a.name for a in found}
        views = {v["name"]: v for v in o.jobs}
        for name in dict.fromkeys(s.subject for s in signals if s.rule in ABSENT and s.subject not in ran):
            found.append(Activity(self.name, "sql_agent_job", name, describe(views.get(name, {})), "missing"))
        due = {e.job: last_deadline(e.by, e.days, now) for e in self.cfg.expect if e.env == self.name}
        for a in found:
            at = due.get(a.name)
            if at and at >= since:
                a.due = at.isoformat()[:19]
        return Result(self.name, self.kind, o.server, found, signals, looked, remembered, len(o.jobs), o.unread)
