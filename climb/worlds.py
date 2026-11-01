"""Fake mornings with planted problems. Each scenario knows what really needs attention and what to fix first, so that a
reader of the tool, or the tool's own report, can be graded on it."""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from conftest import job, run_rows, step  # noqa: E402

from ssisbliss.ssis.fake import FakeCatalog  # noqa: E402

TODAY = date.today()
NOW = f"{TODAY.isoformat()}T08:10:00"
SSIS = '/ISSERVER "\\"\\SSISDB\\ETL\\ETL\\{}.dtsx\\""'


def day(back: int = 0) -> str:
    return (TODAY - timedelta(days=back)).isoformat()


def hhmmss(s: int) -> int:
    return s // 3600 * 10000 + s % 3600 // 60 * 100 + s % 60


@dataclass
class Server:
    name: str
    jobs: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    history: list = field(default_factory=list)
    executions: list = field(default_factory=list)    # (id, package, caller, message), failed tonight
    n: int = 100

    def add(self, name: str, kind: str = "SSIS", command: str | None = None, schedules: int = 1, on_fail: int = 2,
            start: str = "01:00:00", took: int = 600, nights: int = 8, status: int = 1, failed_nights: int = 0,
            today: bool = True, steps: tuple = (), message: str | None = None, running_since: str | None = None, last_ok: int = 0):
        """A job with `nights` of history. failed_nights: it has failed that many nights running, tonight included."""
        cmd = command or (SSIS.format(name.replace(" ", "_")) if kind == "SSIS" else "EXEC dbo.work")
        self.steps.append(step(name, 1, "Run", kind, cmd, on_fail=on_fail))
        first = 0 if today else 1
        self.n += nights
        for back in range(first, nights):     # the newest run has the highest id, as in sysjobhistory
            bad = back < failed_nights or (back == 0 and status == 0)
            rows = steps if back == 0 and steps else ((1, "Run", 0 if bad else 1, message or "The step failed.") if bad else (1, "Run", 1, "ok"),)
            self.history += run_rows(name, self.n - back, f"{day(back)}T{start}", status=0 if bad else 1, duration=hhmmss(took), steps=rows)
        latest = 0 if today else 1
        bad = failed_nights > 0 or status == 0
        self.jobs.append(job(name, f"{day(latest)}T{start}", status=0 if bad and latest == 0 else 1, duration=hhmmss(took), schedules=schedules,
                             message=message, running_since=running_since))

    def save(self, root: Path) -> str:
        d = root / self.name
        d.mkdir(parents=True)
        cat = FakeCatalog("LAB\\" + self.name.upper(), major=15, user="LAB\\alice")
        start = f"{day()}T01:30:00"
        cat.execution_rows = [{"execution_id": i, "folder_name": "ETL", "project_name": "ETL", "package_name": p, "status": 4,
                               "environment_folder_name": None, "environment_name": None, "caller_name": c, "executed_as_name": c,
                               "start_time": start, "end_time": start, "duration_s": 5, "server_name": "S"} for i, p, c, _ in self.executions]
        cat.event_rows = {i: [{"event_message_id": 1, "message_time": "", "message_type": 120, "message_source_type": 40, "subcomponent_name": None,
                               "execution_path": "", "message_source_name": "Load", "package_name": p, "package_path": "\\Package\\Load",
                               "message": f"Load:Error: {m}"}] for i, p, _, m in self.executions}
        cat.save(d)
        maintenance = job("SSIS Server Maintenance Job", f"{day()}T00:00:10")
        (d / "msdb.json").write_text(json.dumps({"server_name": cat.info.server_name, "jobs": [*self.jobs, maintenance], "steps": self.steps,
                                                 "history": self.history}))
        return f"fake://{d}"


def chained(s: Server, *names: str) -> None:
    """Each job starts the next one when it succeeds."""
    for a, b in zip(names, names[1:]):
        s.steps.append(step(a, 2, f"Start {b}", "TSQL", f"EXEC msdb.dbo.sp_start_job @job_name = N'{b}';"))


@dataclass
class Scenario:
    name: str
    build: object                                  # (root) -> list of servers, or (servers, fabric state)
    attention: tuple[str, ...]                     # what really needs someone
    first: str | None                              # what to fix first
    toml: str = ""
    gone: tuple[str, ...] = ()                     # servers that stop answering after they were discovered
    note: str = ""


def _quiet(root):
    s = Server("prod")
    for i, n in enumerate(("Bronze load", "Silver load", "Gold marts", "Fetch prices", "Archive files")):
        s.add(n, start=f"0{i + 1}:00:00")
    return [s]


def _root_cause(root):
    s = Server("prod")
    s.add("1 Bronze", start="00:30:00", took=900)
    s.add("2 Silver", start="00:50:00", took=240, nights=8, status=0, message="The step failed. Execution ID: 77")
    s.add("3 Gold", start="01:30:00", schedules=0, today=False)
    s.add("4 Marts", start="02:00:00", schedules=0, today=False)
    chained(s, "1 Bronze", "2 Silver", "3 Gold", "4 Marts")
    s.executions = [(77, "2_Silver.dtsx", "NT SERVICE\\SQLAgent", "Failed to update database SILVER because the database is read-only.")]
    return [s]


def _new_vs_ongoing(root):
    s = Server("prod")
    s.add("Legacy export", start="00:30:00", failed_nights=6, nights=8, message="Could not find file \\\\old\\share\\export.csv")
    s.add("Sales load", start="01:30:00", status=0, nights=8, message="Violation of PRIMARY KEY constraint PK_Sales")
    s.add("Customer load", start="02:30:00")
    return [s]


def _stuck(root):
    s = Server("prod")
    s.add("Bronze load", start="00:30:00")
    s.add("Reindex warehouse", start="01:00:00", took=2400, today=False, running_since=f"{day()}T01:00:00", nights=8)
    return [s]


def _stale_pile(root):
    s = Server("prod")
    for i, n in enumerate(("Old extract", "Migration 2024", "One-off fix", "Retired feed")):
        s.jobs.append(job(n, f"{day(20 + i)}T02:00:00", status=0, schedules=0, duration=500))
        s.steps.append(step(n, 1, "Run", "TSQL", "EXEC x"))
        s.history += run_rows(n, 500 - i, f"{day(20 + i)}T02:00:00", status=0, steps=[(1, "Run", 0, "The step failed.")])
    s.add("Archive files", "PowerShell", "C:\\etl\\archive.ps1", start="03:00:00", on_fail=3, steps=((1, "Run", 0, "Access denied to \\\\files\\archive"),))
    s.add("Bronze load", start="01:00:00")
    return [s]


def _person_test(root):
    s = Server("prod")
    s.add("Daily extract", start="02:00:00", took=300, today=False, nights=8)
    s.add("Bronze load", start="01:00:00")
    s.executions = [(88, "Scratch_copy.dtsx", "LAB\\dave", "Invalid object name dbo.tmp_dave")]
    return [s]


def _server_down(root):
    a, b = Server("prod"), Server("reporting")
    a.add("Bronze load")
    b.add("Report extract")
    return [a, b]


def _fabric(root):
    s = Server("prod")
    s.add("Bronze load")
    ws = {"id": "w1", "displayName": "Sales", "type": "Workspace"}
    items = [{"id": "m1", "displayName": "Sales model", "type": "SemanticModel"}, {"id": "p1", "displayName": "Load sales", "type": "DataPipeline"}]
    def utc(back, h):
        return f"{day(back)}T0{h}:00:00Z"
    refreshes = [{"status": "Failed", "refreshType": "Scheduled", "startTime": utc(0, 5), "endTime": f"{day()}T05:02:00Z",
                  "serviceExceptionJson": '{"errorCode":"ModelRefreshFailed_CredentialsNotSpecified"}'}]
    refreshes += [{"status": "Completed", "refreshType": "Scheduled", "startTime": utc(b, 5), "endTime": f"{day(b)}T05:30:00Z"} for b in range(1, 6)]
    jobs = [{"status": "Completed", "invokeType": "Scheduled", "startTimeUtc": f"{day(b)}T04:00:00", "endTimeUtc": f"{day(b)}T04:20:00"} for b in range(0, 5)]
    return [s], {"workspaces": [ws], "items": {"w1": items}, "jobs": {"p1": jobs}, "refreshes": {"m1": refreshes}}


def _recovered(root):
    s = Server("prod")
    s.add("Bronze load", start="01:00:00")
    s.steps.append(step("Silver load", 1, "Run", "SSIS", SSIS.format("Silver_load")))
    s.history += run_rows("Silver load", 900, f"{day()}T03:00:00", duration=hhmmss(600), steps=[(1, "Run", 1, "ok")])
    s.history += run_rows("Silver load", 899, f"{day()}T02:00:00", status=0, duration=hhmmss(30), steps=[(1, "Run", 0, "Deadlock victim")])
    for b in range(1, 7):
        s.history += run_rows("Silver load", 800 - b, f"{day(b)}T02:00:00", duration=hhmmss(600), steps=[(1, "Run", 1, "ok")])
    s.jobs.append(job("Silver load", f"{day()}T03:00:00", duration=hhmmss(600)))
    return [s]


def _blast_radius(root):
    s = Server("prod")
    s.add("Fetch rates", start="00:10:00", status=0, nights=8, message="Connection to rates.example.com timed out")
    s.add("1 Bronze", start="00:30:00", took=600)
    s.add("2 Silver", start="00:45:00", took=120, status=0, nights=8, message="Arithmetic overflow converting to numeric")
    s.add("3 Gold", start="01:00:00", schedules=0, today=False)
    s.add("4 Finance", start="01:30:00", schedules=0, today=False)
    chained(s, "1 Bronze", "2 Silver", "3 Gold", "4 Finance")
    return [s]


SCENARIOS = [
    Scenario("quiet-night", _quiet, (), None),
    Scenario("root-cause", _root_cause, ("2 Silver", "3 Gold"), "2 Silver", toml='[[expect]]\nenv = "@prod"\njob = "3 Gold"\nby = "06:00"\n'),
    Scenario("new-vs-ongoing", _new_vs_ongoing, ("Legacy export", "Sales load"), "Sales load", note="one has failed six nights running"),
    Scenario("stuck-job", _stuck, ("Reindex warehouse",), "Reindex warehouse"),
    Scenario("stale-pile", _stale_pile, ("Old extract", "Migration 2024", "One-off fix", "Retired feed", "Archive files"), "Archive files"),
    Scenario("person-test-run", _person_test, ("Daily extract", "Scratch_copy"), "Daily extract", note="a person's test against production"),
    Scenario("server-down", _server_down, ("reporting",), "reporting", gone=("reporting",)),
    Scenario("fabric-refresh", _fabric, ("Sales model",), "Sales model"),
    Scenario("recovered", _recovered, (), None),
    Scenario("blast-radius", _blast_radius, ("Fetch rates", "2 Silver"), "2 Silver", note="one failure blocks two jobs"),
]


# Written after every change was made, and never used to make one: does this hold beyond the mornings it was tuned on?
def _two_servers(root):
    a, b = Server("prod"), Server("reporting")
    a.add("Daily load", start="01:00:00", status=0, message="Timeout expired on dbo.Orders")
    b.add("Daily load", start="01:00:00")
    return [a, b]


def _stuck_blocks_deadline(root):
    s = Server("prod")
    s.add("1 Bronze", start="00:30:00", took=600)
    s.add("2 Silver", start="01:00:00", took=1200, today=False, running_since=f"{day()}T01:00:00")
    s.add("3 Gold", start="02:00:00", schedules=0, today=False)
    chained(s, "1 Bronze", "2 Silver", "3 Gold")
    return [s]


def _mixed(root):
    s = Server("prod")
    s.add("Legacy export", start="00:30:00", failed_nights=4, message="Could not find file")
    s.add("Bronze load", start="01:00:00")
    s.executions = [(91, "Try_new_mapping.dtsx", "LAB\\anna", "Invalid column name Amount2")]
    ws = {"id": "w1", "displayName": "Sales", "type": "Workspace"}
    items = [{"id": "p1", "displayName": "Load sales", "type": "DataPipeline"}]
    jobs = [{"status": "Failed", "invokeType": "Scheduled", "startTimeUtc": f"{day()}T04:00:00", "endTimeUtc": f"{day()}T04:02:00",
             "failureReason": {"errorCode": "JobFailed", "message": "Copy activity failed: source not found"}}]
    jobs += [{"status": "Completed", "invokeType": "Scheduled", "startTimeUtc": f"{day(b)}T04:00:00", "endTimeUtc": f"{day(b)}T04:20:00"} for b in range(1, 6)]
    return [s], {"workspaces": [ws], "items": {"w1": items}, "jobs": {"p1": jobs}}


def _only_old(root):
    s = Server("prod")
    s.jobs.append(job("Year end close", f"{day(30)}T02:00:00", status=0, schedules=0, duration=500))
    s.steps.append(step("Year end close", 1, "Run", "TSQL", "EXEC x"))
    s.history += run_rows("Year end close", 500, f"{day(30)}T02:00:00", status=0, steps=[(1, "Run", 0, "The step failed.")])
    s.add("Bronze load", start="01:00:00")
    s.add("Silver load", start="02:00:00")
    return [s]


HELD_OUT = [
    Scenario("two-servers", _two_servers, ("Daily load",), "Daily load"),
    Scenario("stuck-blocks-deadline", _stuck_blocks_deadline, ("2 Silver", "3 Gold"), "2 Silver", toml='[[expect]]\nenv = "@prod"\njob = "3 Gold"\nby = "06:00"\n'),
    Scenario("mixed", _mixed, ("Load sales", "Legacy export", "Try_new_mapping"), "Load sales"),
    Scenario("only-old", _only_old, ("Year end close",), "Year end close"),
]


def build(s: Scenario, root: Path) -> dict:
    """The world on disk, and the environment that makes the CLI see it."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    built = s.build(root)
    servers, fabric = built if isinstance(built, tuple) else (built, {})
    urls = {x.name: x.save(root / "servers") for x in servers}
    (root / "fabric.json").write_text(json.dumps(fabric))
    work = root / "work"
    work.mkdir()
    (work / "loadwatch.toml").write_text(s.toml.replace("@prod", urls.get("prod", "")))
    env = {"LOADWATCH_SERVERS": ",".join(urls.values()), "LOADWATCH_HOME": str(root / "state"), "LOADWATCH_NOW": NOW,
           "LOADWATCH_FAKE_FABRIC": str(root / "fabric.json"), "SSISBLISS_FAKE_USER": "LAB\\me"}
    return {"work": work, "env": env, "urls": urls, "root": root}


def stop_answering(world: dict, s: Scenario) -> None:
    for name in s.gone:
        (world["root"] / "servers" / name / "msdb.json").unlink()
        (world["root"] / "servers" / name / "state.json").unlink()


__all__ = ["SCENARIOS", "NOW", "Scenario", "build", "datetime", "stop_answering"]
