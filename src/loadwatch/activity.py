"""What every source reports, whatever it watches: SQL Agent jobs today, Fabric pipelines and semantic model refreshes
later. The report and the alerts only know these shapes, so a new source adds a module and nothing else."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

# A run's status, in every source's terms. Sources map their own words to these.
SUCCEEDED, FAILED, CANCELED, RUNNING = "succeeded", "failed", "canceled", "running"


@dataclass
class Signal:
    env: str                 # the source's name
    rule: str
    subject: str
    severity: str            # critical, warning, info
    message: str
    next: str | None = None  # the command that says more
    at: str | None = None    # when it happened, if it is an event: what comes first depends on it
    in_a_row: int = 0        # how many runs in a row have failed; 1 is new
    blocks: list[str] = field(default_factory=list)   # what did not run because of it
    person: str | None = None    # run by this person, not by a schedule: probably a test


def priority(alert: dict, now: datetime, window_hours: float = 24) -> tuple:
    """What to look at first. What cannot be seen at all comes first, since it may hide anything; then what happened lately before
    what is old, what needs someone before what is only worth a look, what blocks others, what is new before what has been failing
    for a while, and a person's test run last. Lower comes first."""
    blind = alert["rule"] == "server-unreachable"
    at = alert.get("at")
    recent = at is None or now - datetime.fromisoformat(at[:19]) <= timedelta(hours=window_hours)
    severity = {"critical": 0, "warning": 1}.get(alert["severity"], 2)
    return (not blind, bool(alert.get("person")), not recent, severity, -len(alert.get("blocks") or []),
            max(alert.get("in_a_row") or 1, 1), alert.get("opened_at") or "")


def why_first(alert: dict) -> str:
    """Why this one, in words."""
    if alert["rule"] == "server-unreachable":
        return "while it does not answer, nothing is known about what runs on it"
    blocks = alert.get("blocks") or []
    if blocks:
        return f"{', '.join(blocks)} did not run because of it"
    n = alert.get("in_a_row") or 0
    if n == 1:
        return "it is new tonight"
    return "it is the most recent and most serious"


@dataclass
class Run:
    start: str
    status: str
    duration_s: int | None = None


@dataclass
class Activity:
    """Something that ran in the period: a job, a pipeline, a refresh."""
    source: str
    kind: str                      # sql_agent_job; later fabric_pipeline, semantic_model_refresh
    name: str
    what: str                      # what it runs, in words
    state: str                     # ok, failed, slow, suspect (says it worked, and does not look it), running, missing (should
                                   # have run and did not), recovered (failed, then succeeded in the period)
    runs: list[Run] = field(default_factory=list)   # newest first
    usual_s: float | None = None   # its usual duration
    more_runs: bool = False        # it ran more often than was read
    due: str | None = None         # the deadline that applies in the period


@dataclass
class Result:
    """One source, one pass."""
    source: str
    kind: str
    location: str | None                      # the server, the workspace
    activities: list[Activity] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    looked: set[str] = field(default_factory=set)      # the rules that really looked: only they can clear an alert
    memory: dict[str, dict] = field(default_factory=dict)
    watched: int = 0                          # how many things it knows of, ran or not
    unread: dict[str, str] = field(default_factory=dict)


class Source(Protocol):
    name: str
    kind: str

    def collect(self, since: datetime, now: datetime, memory: dict[str, dict], thorough: bool) -> Result:
        """What happened since `since`. thorough: this pass may read everything (a report); otherwise only what
        changed (a check every few minutes). memory is what this source asked to keep at the last pass."""
        ...
