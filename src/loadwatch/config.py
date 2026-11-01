"""loadwatch.toml, which is optional: without one, loadwatch works out what to watch itself (discover.py). The file adds what
no server knows, the deadlines, and can name the environments to watch instead of discovering them. Which server an
environment is, and how to log in to it, stays in ssisbliss.toml, so one estate has one description. Secrets stay out of
files: the webhook URL comes from the environment."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

FILE = "loadwatch.toml"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass
class Expectation:
    """`job` has succeeded by `by` (HH:MM, the server's clock) on `days`."""
    env: str
    job: str
    by: str
    days: tuple[str, ...] = DAYS


@dataclass
class Config:
    environments: list[str] = field(default_factory=list)   # empty: discover what to watch
    db: Path = Path("~/.loadwatch/loadwatch.db")
    ssisbliss_config: str | None = None
    window_hours: float = 24.0          # how far back a failed execution counts
    overrun_factor: float = 2.0         # a run is too long at this many times its usual duration
    overrun_minutes: float = 10.0       # and at least this much longer than usual
    min_runs: int = 3                   # finished runs needed to know what is usual
    grace_minutes: float = 30.0         # msdb refreshes next_run every 20 minutes
    remind_hours: float = 12.0          # an alert nobody acknowledged is sent again after this long
    expect: list[Expectation] = field(default_factory=list)
    webhook: str | None = None          # Slack or Teams incoming webhook, from $LOADWATCH_WEBHOOK
    heartbeat: str | None = None        # pinged after a pass, so a dead monitor is noticed by someone else


def home() -> Path:
    """Where loadwatch keeps its state: ~/.loadwatch, or $LOADWATCH_HOME."""
    return Path(os.environ.get("LOADWATCH_HOME") or "~/.loadwatch").expanduser()


def find(path: str | None = None) -> Path | None:
    """--config must exist; the usual places may not, and need not."""
    if path:
        if not Path(path).is_file():
            raise ValueError(f"no {FILE} at {path}")
        return Path(path)
    return next((c for c in (Path(FILE), Path("~/.config/loadwatch").expanduser() / FILE) if c.is_file()), None)


def load(path: str | None = None) -> Config:
    file = find(path)
    if file is None:
        c = Config(db=home() / "loadwatch.db")
        c.webhook = os.environ.get("LOADWATCH_WEBHOOK")
        return c
    raw = tomllib.loads(file.read_text())
    known = {"db", "environments", "ssisbliss_config", "window_hours", "overrun_factor", "overrun_minutes", "min_runs",
             "grace_minutes", "remind_hours", "heartbeat"}
    c = Config(**{"db": home() / "loadwatch.db", **{k: v for k, v in raw.items() if k in known}})
    c.db = Path(c.db).expanduser()
    if c.ssisbliss_config:
        c.ssisbliss_config = str((file.parent / Path(c.ssisbliss_config).expanduser()).resolve())
    for e in raw.get("expect", []):
        days = tuple(d.lower()[:3] for d in e.get("days", DAYS))
        if set(days) - set(DAYS):
            raise ValueError(f"{file}: expect {e.get('job')}: days are {', '.join(DAYS)}")
        c.expect.append(Expectation(e["env"], e["job"], e["by"], days))
    c.webhook = os.environ.get("LOADWATCH_WEBHOOK")
    return c
