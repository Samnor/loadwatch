"""What every source reports, whatever it watches: SQL Agent jobs today, Fabric pipelines and semantic model refreshes
later. The report and the alerts only know these shapes, so a new source adds a module and nothing else."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
