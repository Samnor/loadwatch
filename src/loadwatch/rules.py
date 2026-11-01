"""From what a pass saw, and what earlier passes remember, to signals. Pure functions: no server, no clock, no
database. ssisbliss answers "what is true now"; these rules add what only watching over time can tell: what
should have happened and did not, and what looks fine and is not."""
from __future__ import annotations

import re
from argparse import Namespace
from dataclasses import asdict
from datetime import datetime, time, timedelta
from statistics import median

from ssisbliss.ssis.contract import _with_target

from .activity import Signal
from .config import DAYS, Config, Expectation
from .observe import Observation

SCRIPT_STEPS = {"PowerShell", "CmdExec"}
_OUTPUT_ERROR = re.compile(r"Traceback \(most recent call last\)|\bException\b|\bFullyQualifiedErrorId\b|\bERROR:", re.I)
# The part of a pass a rule reads. A rule whose part could not be read did not look, so it cannot clear an alert:
# no answer from a server is not good news.
RULES = {"server-unreachable": None, "blind-spot": None, "job-failed": "jobs", "deadline-missed": "jobs",
         "missed-schedule": "jobs", "job-unscheduled": "jobs", "overrunning": "history", "overdue": "history",
         "ran-long": "history", "step-failed-silently": "history",
         "script-output-error": "history", "slower": "history", "chain-stopped": "chains", "chain-missing-job": "chains",
         "ssis-failed": "executions", "health": "health"}


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text[:19])


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.0f} min"


def last_deadline(by: str, days: tuple[str, ...], now: datetime) -> datetime | None:
    """The latest moment `by` on one of `days` that has passed."""
    hour, minute = map(int, by.split(":"))
    for back in range(8):
        day = now.date() - timedelta(days=back)
        at = datetime.combine(day, time(hour, minute))
        if DAYS[day.weekday()] in days and at <= now:
            return at
    return None


def _deadline(o: Observation, e: Expectation, view: dict | None, now: datetime) -> Signal | None:
    at = last_deadline(e.by, e.days, now)
    if at is None:
        return None
    if view is None:
        return Signal(o.env, "deadline-missed", e.job, "critical", f"{e.job} should have succeeded by {e.by}, and there is "
                      f"no such job on {o.server or o.env}")
    last = view.get("last_run") or {}
    if last.get("outcome") == "succeeded" and last.get("start") and _at(last["start"]) >= at - timedelta(hours=24):
        return None
    state = (f"it has been running since {view['running_since']}" if view["running"]
             else f"its last run was {last['outcome']} at {last['start']}" if last else "it has never run")
    return Signal(o.env, "deadline-missed", e.job, "critical", f"{e.job} should have succeeded by {e.by}: {state}",
                  f'ssisbliss history "{e.job}" --runs 5')


def _package(failure: dict | None) -> str:
    """What ssisbliss found out about the failed package, in a sentence."""
    if not failure:
        return ""
    task = next(iter(failure.get("failed_tasks") or []), {})
    known = next(iter(task.get("explained") or []), {})
    where = f" Package task {task['task']}" if task.get("task") else ""
    on = f" on database {task['database']}" if task.get("database") else ""
    said = re.sub(r"^.*?:Error:\s*", "", next(iter(task.get("messages") or []), "")).rstrip(".")
    likely = f" Likely: {known['cause']}" if known.get("cause") else ""
    return f"{where}{on}: {said[:300]}.{likely}" if where else ""


def _failed(o: Observation, v: dict, prev: dict, severity: str) -> Signal:
    """severity: an old failure stays open, but last night's come first."""
    last = v["last_run"]
    if v["name"] not in o.histories and prev.get("failure"):   # the same failed run as the last pass, which read it
        return Signal(o.env, "job-failed", v["name"], severity, *prev["failure"])
    run = next(iter(o.histories.get(v["name"], [])), {})
    step = next((s for s in run.get("steps", []) if s["outcome"] in ("failed", "canceled")), None)
    defined = {s["step_id"]: s for s in v.get("steps", [])}.get(step["step_id"], {}) if step else {}
    where = f": step {step['step_id']} ({step['name']})" if step else ""
    text = (step or {}).get("message") or last.get("message") or ""
    script = (f" A {defined['subsystem']} step: SQL Agent keeps only the end of its output, so the script's own log has "
              f"the rest." if defined.get("subsystem") in SCRIPT_STEPS else "")
    detail = _package(o.failures.get((step or {}).get("ssis_execution_id")))
    return Signal(o.env, "job-failed", v["name"], severity, f"{v['name']} {last['outcome']} at {last['start']}{where}. "
                  f"{text[:300]}{script}{detail}", (step or {}).get("next") or v.get("next"))


def _jobs(o: Observation, cfg: Config, memory: dict[str, dict], now: datetime) -> tuple[list[Signal], dict[str, dict]]:
    out, remembered = [], {}
    grace = timedelta(minutes=cfg.grace_minutes)
    for v in o.jobs:
        name, last = v["name"], v.get("last_run")
        prev = memory.get(name, {})
        started = max(filter(None, [(last or {}).get("start"), v.get("running_since")]), default=None)
        failure = None
        if last and last["outcome"] in ("failed", "canceled") and v["enabled"]:
            recent = now - _at(last["start"]) <= timedelta(hours=cfg.window_hours)
            s = _failed(o, v, prev, "critical" if recent else "warning")
            out.append(s)
            failure = [s.message, s.next]

        # A schedule that passed without a run shows nowhere on the server: next_run has simply moved on.
        missed, due = prev.get("missed"), prev.get("next_run")
        if due and prev.get("active") and _at(due) + grace <= now and not (started and started[:19] >= due[:19]):
            missed = missed or due
        if missed and started and started[:19] >= missed[:19]:
            missed = None
        if missed:
            out.append(Signal(o.env, "missed-schedule", name, "warning", f"{name} was due at {missed} and did not start",
                              "ssisbliss jobs --brief"))

        # Someone turned it off. Sticky until it is back on or the alert is acknowledged: nothing else will ever say so.
        active = bool(v["enabled"] and v.get("schedules"))
        quiet = None if active else prev.get("quiet_since") or (now.isoformat()[:19] if prev.get("active") else None)
        if quiet:
            what = "disabled" if not v["enabled"] else "left without an enabled schedule"
            out.append(Signal(o.env, "job-unscheduled", name, "warning", f"{name} was {what} (seen at {quiet}): it will not "
                              f"run again until someone turns it back on. Acknowledge this if it was meant"))
        runs = o.histories.get(name, [])
        usual = [r["duration_s"] for r in runs if r.get("outcome") == "succeeded" and r.get("duration_s") is not None]
        if v["running"] and v.get("running_since") and len(usual) >= cfg.min_runs:
            took, typical = (now - _at(v["running_since"])).total_seconds(), median(usual)
            if took >= cfg.overrun_factor * typical and took - typical >= cfg.overrun_minutes * 60:
                out.append(Signal(o.env, "overrunning", name, "warning", f"{name} has run for {_minutes(took)}; it usually takes "
                                  f"{_minutes(typical)} (median of {len(usual)} runs)", "ssisbliss chain"))
        # What the job's history says only counts when it was read; the next pass, which does not read it, must not
        # take that for the problem having gone. It holds until history is read again.
        kept = [Signal(**s) for s in prev.get("from_history", [])]
        if name in o.histories:
            kept = _silent(o, v, runs[0]) if runs and runs[0].get("outcome") == "succeeded" else []
            kept += _overdue(o, cfg, v, runs, active, missed, now) + _ran_long(o, cfg, v, runs)
        out += kept
        ids = [s["ssis_execution_id"] for r in runs for s in r.get("steps", [])
               if s["outcome"] != "succeeded" and s.get("ssis_execution_id")]
        remembered[name] = {"next_run": v.get("next_run") or due, "active": active, "last_start": (last or {}).get("start"),
                            "missed": missed, "quiet_since": quiet, "failure": failure,
                            "from_history": [asdict(s) for s in kept],
                            "failed_executions": ids if name in o.histories else prev.get("failed_executions", [])}
    return out, remembered


def _ran_long(o: Observation, cfg: Config, v: dict, runs: list[dict]) -> list[Signal]:
    """The newest run took far longer than the job usually does: it worked, this time."""
    took = [r["duration_s"] for r in runs if r.get("outcome") == "succeeded" and r.get("duration_s") is not None]
    if not runs or runs[0].get("outcome") != "succeeded" or runs[0].get("duration_s") is None or len(took) <= cfg.min_runs:
        return []
    usual, last = median(took[1:]), runs[0]["duration_s"]
    if last < cfg.overrun_factor * usual or last - usual < cfg.overrun_minutes * 60:
        return []
    return [Signal(o.env, "ran-long", v["name"], "warning", f"{v['name']} took {_minutes(last)} at {runs[0]['start']}; it usually takes "
                   f"{_minutes(usual)}. Anything after it started late", f'ssisbliss history "{v["name"]}" --runs 10')]


def _overdue(o: Observation, cfg: Config, v: dict, runs: list[dict], active: bool, missed: str | None,
             now: datetime) -> list[Signal]:
    """Longer without a run than it has ever gone in the runs read, with no memory needed: a report run once in the
    morning sees a daily job that did not run. Against the longest gap, so a job that skips weekends is not late on Sunday."""
    starts = [_at(r["start"]) for r in runs if r.get("start")]
    if not active or missed or v["running"] or len(starts) <= cfg.min_runs:
        return []
    longest = max((a - b).total_seconds() for a, b in zip(starts, starts[1:]))
    quiet = (now - starts[0]).total_seconds()
    if longest <= 0 or quiet < 1.1 * longest + cfg.grace_minutes * 60:
        return []
    return [Signal(o.env, "overdue", v["name"], "warning", f"{v['name']} last started at {starts[0].isoformat()}: {quiet / 3600:.1f} "
                   f"hours ago, and it has not gone more than {longest / 3600:.1f} hours between runs lately",
                   "ssisbliss jobs --brief")]


def _silent(o: Observation, v: dict, run: dict) -> list[Signal]:
    """The job says it succeeded. These say it did not, or not all of it."""
    out = []
    defined = {s["step_id"]: s for s in v.get("steps", [])}
    for s in run.get("steps", []):
        subject = f"{v['name']} / {s['name']}"
        if s["outcome"] in ("failed", "canceled"):
            out.append(Signal(o.env, "step-failed-silently", subject, "warning", f"{v['name']} reports success, but step "
                              f"{s['step_id']} ({s['name']}) failed and the job went on: {(s.get('message') or '')[:300]}",
                              s.get("next")))
        elif defined.get(s["step_id"], {}).get("subsystem") in SCRIPT_STEPS and _OUTPUT_ERROR.search(s.get("message") or ""):
            out.append(Signal(o.env, "script-output-error", subject, "warning", f"{v['name']}: {s['name']} exited 0, but its "
                              f"output looks like a failure: {(s.get('message') or '')[:300]}"))
        if s.get("slower"):
            out.append(Signal(o.env, "slower", subject, "info", f"{v['name']}: {s['name']} {s['slower']}", s.get("next")))
    return out


def _chains(o: Observation) -> list[Signal]:
    out = []
    for c in o.chains:
        if c["verdict"].startswith("stopped after"):
            out.append(Signal(o.env, "chain-stopped", c["root"], "critical", f"chain {c['root']} {c['verdict']}. {c.get('why', '')}"
                              .strip(), f'ssisbliss chain --name "{c["root"]}"'))
        for job in c.get("missing_jobs", []):
            out.append(Signal(o.env, "chain-missing-job", f"{c['root']} / {job}", "critical",
                              f"chain {c['root']} starts {job}, and there is no such job"))
    return out


def _executions(o: Observation, remembered: dict[str, dict]) -> list[Signal]:
    """A failed package no failed job step accounts for: started by a person, or by a job that does not wait for it
    (catalog.start_execution without SYNCHRONIZED), where the job reports success whatever the package does."""
    linked = {i for m in remembered.values() for i in m.get("failed_executions", [])}
    out, seen = [], set()
    for e in o.failed_executions:     # newest first
        subject = f"{e['folder']}/{e['project']}/{e['package']}"
        if e["execution_id"] in linked or subject in seen:
            continue
        seen.add(subject)
        task = next(iter(e.get("failed_tasks") or []), {})
        text = next(iter(task.get("messages") or e.get("package_errors") or []), "no message logged")
        where = f" in {task['task']}" if task.get("task") else ""
        on = f" on database {task['database']}" if task.get("database") else ""
        known = next(iter(task.get("explained") or []), {})
        cause = f" Likely: {known['cause']}" if known.get("cause") else ""
        out.append(Signal(o.env, "ssis-failed", subject, "warning", f"{subject} failed at {str(e['start_time'])[:19]}{where}{on}, started by "
                          f"{e.get('caller')}, and no job failed with it: {text[:300]}{cause}",
                          f"ssisbliss executions --id {e['execution_id']}"))
    return out


def evaluate(o: Observation, cfg: Config, memory: dict[str, dict], now: datetime
             ) -> tuple[list[Signal], set[str], dict[str, dict]]:
    """(signals, the rules that really looked, what to remember for the next pass)."""
    if not o.read("jobs"):
        s = Signal(o.env, "server-unreachable", o.env, "critical", f"could not read {o.env}: {o.unread['jobs'][:300]}")
        return [s], {"server-unreachable", "blind-spot"}, memory
    signals, remembered = _jobs(o, cfg, memory, now)
    views = {v["name"]: v for v in o.jobs}
    signals += [s for e in cfg.expect if e.env == o.env if (s := _deadline(o, e, views.get(e.job), now))]
    if o.read("chains"):
        signals += _chains(o)
    if o.read("executions"):
        signals += _executions(o, remembered)
    if o.read("health"):
        signals += [Signal(o.env, "health", f["check"], "warning", f["message"], "ssisbliss health")
                    for f in o.health if f["severity"] == "warning"]
    signals += [Signal(o.env, "blind-spot", part, "warning", f"could not read {part} on {o.env}, so nothing is known about "
                       f"it: {why[:300]}") for part, why in o.unread.items()]
    looked = {r for r, part in RULES.items() if part is None or o.read(part)}
    if not o.thorough:
        looked -= {"overdue"}    # needs every job's history: a quick pass has not looked
    for s in signals:
        if s.next:     # the same server the signal is about
            flag, value = (o.target + ["", ""])[:2]
            s.next = _with_target(s.next, Namespace(env=value if flag == "--env" else None, server=value if flag == "--server" else None))
    return signals, looked, {**memory, **remembered}
