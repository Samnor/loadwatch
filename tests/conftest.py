"""A fake estate on ssisbliss's own fake:// servers: msdb.json and a fake catalog in one folder, a ssisbliss.toml and a
loadwatch.toml. Tests change the rows and run `check` with a clock, as a scheduler would run it over a night."""
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest
from ssisbliss.ssis.fake import FakeCatalog

from loadwatch import cli


def job(name, last=None, status=1, duration=500, enabled=True, schedules=1, next_run=None, running_since=None, message=None):
    return {"name": name, "enabled": enabled, "category": "ETL", "description": None, "owner": "sa", "running": bool(running_since),
            "running_since": running_since, "last_executed_step_id": None, "next_run": next_run, "last_start": last,
            "last_status": status if last else None, "last_duration": duration if last else None,
            "last_message": message or ("The job failed." if status == 0 else "The job succeeded."), "enabled_schedules": schedules}


def step(job_name, step_id=1, name="Load", subsystem="SSIS", command='/ISSERVER "\\"\\SSISDB\\F\\P\\Main.dtsx\\""', on_fail=2):
    return {"job": job_name, "step_id": step_id, "step_name": name, "subsystem": subsystem, "command": command, "database_name": None,
            "on_success_action": 1, "on_success_step_id": 0, "on_fail_action": on_fail, "on_fail_step_id": 0, "proxy": None}


def run_rows(job_name, run_id, start, status=1, duration=500, steps=()):
    """sysjobhistory rows of one run: its steps as (step_id, name, status, message), then the job outcome."""
    rows = [{"job": job_name, "run_id": run_id, "instance_id": run_id * 10 + i, "step_id": sid, "step_name": name, "run_status": st,
             "run_duration": duration, "start_time": start, "retries_attempted": 0, "message": msg}
            for i, (sid, name, st, msg) in enumerate(steps)]
    return rows + [{"job": job_name, "run_id": run_id, "instance_id": run_id * 10 + 9, "step_id": 0, "step_name": "(Job outcome)",
                    "run_status": status, "run_duration": duration, "start_time": start, "retries_attempted": 0,
                    "message": "The job succeeded." if status == 1 else "The job failed."}]


@dataclass
class Estate:
    root: Path
    jobs: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    history: list = field(default_factory=list)
    runs: int = 0

    @property
    def server(self) -> Path:
        return self.root / "prod"

    def save(self):
        maintenance = job("SSIS Server Maintenance Job", "2026-11-01T00:00:00")     # or ssisbliss's health says it is missing
        (self.server / "msdb.json").write_text(json.dumps({"server_name": "LAB\\PROD", "jobs": [*self.jobs, maintenance],
                                                           "steps": self.steps, "history": self.history}))

    def catalog(self):
        return FakeCatalog.load(self.server)

    def config(self, extra=""):
        (self.root / "loadwatch.toml").write_text(f'environments = ["prod"]\ndb = "{self.root}/watch.db"\n'
                                                  f'ssisbliss_config = "ssisbliss.toml"\n{extra}\n')

    def cli(self, *args):
        return cli.run(["--config", str(self.root / "loadwatch.toml"), *args])

    def check(self, now="2026-11-02T08:00:00"):
        self.save()
        doc, code = self.cli("check", "--now", now)
        assert code == 0, doc
        return doc

    def open(self):
        return {(a["rule"], a["subject"]): a for a in self.cli("alerts")[0]["alerts"]}


@pytest.fixture
def estate(tmp_path):
    e = Estate(tmp_path)
    e.server.mkdir()
    FakeCatalog("LAB\\PROD", major=15, user="LAB\\alice").save(e.server)
    (tmp_path / "ssisbliss.toml").write_text(f'[environments.prod]\nserver = "fake://{e.server}"\n')
    e.config()
    return e


def at(day_hour: str) -> str:
    return datetime.fromisoformat(f"2026-11-{day_hour}").isoformat()[:19]
