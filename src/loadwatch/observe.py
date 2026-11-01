"""What one pass sees on one environment. All of it comes from ssisbliss's commands, in process: their JSON
documents are the API (`run` keeps the output contract and every command is read-only here), so reading msdb
and SSISDB, parsing job steps, chains, known errors, auth and the config of environments stay in ssisbliss."""
from __future__ import annotations

from dataclasses import dataclass, field

from ssisbliss.ssis.cli import run

HISTORY_RUNS = 10
THOROUGH_RUNS = 100       # a report's runs per job: a job every 15 minutes runs 96 times a day


@dataclass
class Observation:
    env: str
    server: str | None = None
    jobs: list[dict] = field(default_factory=list)
    chains: list[dict] = field(default_factory=list)
    health: list[dict] = field(default_factory=list)
    failed_executions: list[dict] = field(default_factory=list)
    histories: dict[str, list[dict]] = field(default_factory=dict)  # job -> its recent runs, newest first
    failures: dict[int, dict] = field(default_factory=dict)         # failed step's SSIS execution -> ssisbliss's report of it
    versions: dict[str, list[dict]] = field(default_factory=dict)   # folder/project with a failure -> its deployed versions, newest first
    history_runs: int = HISTORY_RUNS
    thorough: bool = False    # every job's history was read, not only the ones that changed
    target: list[str] = field(default_factory=list)   # how ssisbliss reaches it: --env NAME, or --server NAME
    unread: dict[str, str] = field(default_factory=dict)            # part that could not be read -> why

    def read(self, part: str) -> bool:
        return part not in self.unread


def ask(target: list[str], *command: str) -> dict:
    """One ssisbliss command against a target (--env NAME or --server NAME ...): its document, with `ok` false when it did
    not work."""
    name, *rest = command
    return run([name, *target, *rest]).doc or {}


def _part(o: Observation, part: str, doc: dict, key: str) -> list:
    if doc.get("ok"):
        return doc.get(key) or []
    o.unread[part] = str(doc.get("error") or doc.get("error_kind") or "no answer")
    return []


def observe(env: str, target: list[str], window_hours: float, seen_starts: dict[str, str | None],
            thorough: bool = False) -> Observation:
    """seen_starts: job -> the last run's start at the previous pass. History is read for the jobs running now and
    the jobs with a new finished run, so a pass costs one query per job that did something. thorough reads every job
    that has ever run, for a report nothing prepared. env: the name it reports under."""
    o = Observation(env, thorough=thorough, history_runs=THOROUGH_RUNS if thorough else HISTORY_RUNS, target=target)
    jobs = ask(target, "jobs")
    o.server = jobs.get("server")
    o.jobs = _part(o, "jobs", jobs, "jobs")
    if not o.read("jobs"):
        return o      # the server did not answer: the other parts would only say the same
    o.chains = _part(o, "chains", ask(target, "chain"), "chains")
    o.health = _part(o, "health", ask(target, "health"), "findings")
    o.failed_executions = _part(o, "executions", ask(target, "executions", "--failed", "--since-hours", str(window_hours)),
                                "executions")
    for v in o.jobs:
        last = (v.get("last_run") or {}).get("start")
        if v["running"] or (last and (thorough or last != seen_starts.get(v["name"]))):
            doc = ask(target, "history", v["name"], "--runs", str(o.history_runs))
            if doc.get("ok"):
                o.histories[v["name"]] = doc.get("runs") or []
            else:
                o.unread["history"] = str(doc.get("error") or "no answer")
    for runs in o.histories.values():    # the package behind a failed step: which task, what error, the usual cause
        for s in (runs[0].get("steps", []) if runs else []):
            if s["outcome"] != "succeeded" and s.get("ssis_execution_id"):
                found = ask(target, "executions", "--id", str(s["ssis_execution_id"])).get("executions")
                if found:
                    o.failures[s["ssis_execution_id"]] = found[0]
    for e in [*o.failures.values(), *o.failed_executions]:     # what changed before it failed
        key = f"{e.get('folder')}/{e.get('project')}"
        if e.get("folder") and key not in o.versions:
            o.versions[key] = ask(target, "versions", "--folder", e["folder"], "--project", e["project"]).get("versions") or []
    return o
