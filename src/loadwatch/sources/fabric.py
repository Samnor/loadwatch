"""A Fabric workspace: its pipelines, notebooks and other scheduled items, and its semantic model refreshes. Tokens come
from the Azure CLI (whoever ran `az login`), so no secret is stored here, and every call only reads.

    GET https://api.fabric.microsoft.com/v1/workspaces
    GET https://api.fabric.microsoft.com/v1/workspaces/{workspace}/items
    GET https://api.fabric.microsoft.com/v1/workspaces/{workspace}/items/{item}/jobs/instances
    GET https://api.powerbi.com/v1.0/myorg/groups/{workspace}/datasets/{model}/refreshes?$top=60

Fabric keeps about the last 100 finished runs of an item. Reading a model's refresh history takes Write permission on it,
and some models have none to give: those are counted as not read, not reported as failed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from ..activity import (
    CANCELED,
    FAILED,
    RUNNING,
    SUCCEEDED,
    Activity,
    Result,
    Run,
    Signal,
)
from ..config import Config

FABRIC = "https://api.fabric.microsoft.com"
POWER_BI = "https://analysis.windows.net/powerbi/api"
POWER_BI_API = "https://api.powerbi.com/v1.0/myorg"
PORTAL = "https://app.fabric.microsoft.com/groups"

# Item types with job history, in words.
JOB_ITEMS = {"DataPipeline": "Data pipeline", "Notebook": "Notebook", "SparkJobDefinition": "Spark job", "Dataflow": "Dataflow",
             "CopyJob": "Copy job", "ApacheAirflowJob": "Airflow job", "DataBuildToolJob": "dbt job"}
MODEL = "SemanticModel"
# Fabric job instances. A deduped run was skipped because one was already going: it never ran.
JOB_STATUS = {"Completed": SUCCEEDED, "Failed": FAILED, "Cancelled": CANCELED, "InProgress": RUNNING, "NotStarted": RUNNING}
# Power BI refresh history. A refresh in progress is reported as Unknown.
REFRESH_STATUS = {"Completed": SUCCEEDED, "Failed": FAILED, "Disabled": CANCELED, "Unknown": RUNNING}
RULES = {"fabric-failed", "ran-long", "overdue", "server-unreachable"}
PARALLEL = 6         # the API allows 500 calls a minute


@dataclass(frozen=True)
class Token:
    value: str
    expires_on: str

    def __repr__(self) -> str:      # never in a log or a traceback
        return f"Token(expires_on={self.expires_on!r})"


def az_token(resource: str, timeout: float = 60) -> Token:
    """An access token for `resource` from the Azure CLI. On Windows az is az.cmd, which `which` finds and a bare
    "az" would not."""
    az = shutil.which("az")
    if az is None:
        raise ValueError("the Azure CLI is not on PATH: install it and run `az login`")
    p = subprocess.run([az, "account", "get-access-token", "--resource", resource, "--output", "json"],
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise ValueError(f"az could not get a token for {resource} (run `az login`): {p.stderr.strip()[:300]}")
    doc = json.loads(p.stdout)
    return Token(doc["accessToken"], doc.get("expiresOn", ""))


class ApiError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(f"{status} {code}")
        self.status, self.code = status, code

    @property
    def transient(self) -> bool:
        return self.status == 429 or self.status >= 500


class FabricClient:
    """GETs with a token from az, backing off when the service says to. A token only ever goes to the two API hosts:
    the next page is asked for with its token, never by following a URL out of a response."""

    def __init__(self, token=None, opener=None, sleep=time.sleep):
        self._get_token, self._open, self._sleep = token or az_token, opener or urllib.request.urlopen, sleep
        self._tokens: dict[str, Token] = {}

    def _token(self, resource: str) -> str:
        if resource not in self._tokens:
            self._tokens[resource] = self._get_token(resource)
        return self._tokens[resource].value

    def get(self, url: str, resource: str = FABRIC) -> dict:
        for attempt in range(5):
            request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token(resource)}"})
            try:
                with self._open(request, timeout=60) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                try:
                    body = json.loads(e.read() or b"{}")
                except ValueError:
                    body = {}
                code = str(body.get("errorCode") or (body.get("error") or {}).get("code") or e.reason)
                if e.code == 429 or (e.code >= 500 and attempt < 2):
                    if attempt == 4:
                        raise ApiError(e.code, code) from None
                    wait = (e.headers.get("Retry-After") if e.headers else None) or 2 ** attempt
                    self._sleep(min(float(wait), 60))
                    continue
                raise ApiError(e.code, code) from None
            except urllib.error.URLError as e:
                raise ApiError(0, str(e.reason)) from None
        raise ApiError(0, "no answer")

    def pages(self, url: str, resource: str = FABRIC) -> list[dict]:
        out, token = [], None
        while True:
            sep = "&" if "?" in url else "?"
            doc = self.get(url + (f"{sep}continuationToken={urllib.parse.quote(token, safe='')}" if token else ""), resource)
            out += doc.get("value", [])
            token = doc.get("continuationToken")
            if not token:
                return out

    def workspaces(self) -> list[dict]:
        return self.pages(f"{FABRIC}/v1/workspaces")

    def items(self, workspace: str) -> list[dict]:
        return self.pages(f"{FABRIC}/v1/workspaces/{workspace}/items")

    def job_runs(self, workspace: str, item: str) -> list[dict]:
        return self.get(f"{FABRIC}/v1/workspaces/{workspace}/items/{item}/jobs/instances").get("value", [])

    def refreshes(self, workspace: str, model: str) -> list[dict]:
        return self.get(f"{POWER_BI_API}/groups/{workspace}/datasets/{model}/refreshes?$top=60", POWER_BI).get("value", [])


class FakeFabric:
    """A tenant from a JSON file, for tests and evaluations: $LOADWATCH_FAKE_FABRIC, as ssisbliss has fake:// servers.
    {"workspaces": [...], "items": {workspace: [...]}, "jobs": {item: [...]}, "refreshes": {item: [...]}}"""

    def __init__(self, path: str):
        self.state = json.loads(Path(path).read_text())

    def workspaces(self) -> list[dict]:
        return self.state.get("workspaces", [])

    def items(self, workspace: str) -> list[dict]:
        return self.state.get("items", {}).get(workspace, [])

    def job_runs(self, workspace: str, item: str) -> list[dict]:
        return self.state.get("jobs", {}).get(item, [])

    def refreshes(self, workspace: str, model: str) -> list[dict]:
        return self.state.get("refreshes", {}).get(model, [])


def client() -> FabricClient:
    fake = os.environ.get("LOADWATCH_FAKE_FABRIC")
    return FakeFabric(fake) if fake else FabricClient()


def watchable(items: list[dict]) -> list[dict]:
    return [i for i in items if i.get("type") in JOB_ITEMS or i.get("type") == MODEL]


def local(utc: str | None) -> str | None:
    """A service's UTC time on this machine's clock, as the rest of the report has it."""
    if not utc:
        return None
    return datetime.fromisoformat(utc.rstrip("Z")[:19]).replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None).isoformat()[:19]


def _seconds(start: str, end: str | None) -> int | None:
    return int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()) if end else None


def job_run(r: dict) -> dict | None:
    if r.get("status") == "Deduped" or not r.get("startTimeUtc"):
        return None
    start, end = local(r["startTimeUtc"]), local(r.get("endTimeUtc"))
    failure = r.get("failureReason") or {}
    return {"start": start, "status": JOB_STATUS.get(r.get("status"), FAILED), "duration_s": _seconds(start, end),
            "scheduled": r.get("invokeType") == "Scheduled",
            "failure": " ".join(x for x in (failure.get("errorCode"), failure.get("message")) if x)}


def refresh_run(r: dict) -> dict | None:
    if not r.get("startTime"):
        return None
    start, end = local(r["startTime"]), local(r.get("endTime"))
    failure = r.get("serviceExceptionJson") or ""
    try:
        failure = " ".join(str(v) for v in json.loads(failure).values()) if failure else ""
    except ValueError:
        pass
    return {"start": start, "status": REFRESH_STATUS.get(r.get("status"), FAILED), "duration_s": _seconds(start, end),
            "scheduled": r.get("refreshType") == "Scheduled", "failure": failure}


def judge(source: str, item: dict, runs: list[dict], since: datetime, now: datetime, cfg: Config, url: str,
          ) -> tuple[Activity | None, list[Signal]]:
    """An item's runs, newest first, as an activity and what needs attention. Pure."""
    name, kind = item["displayName"], item["type"]
    what = "Semantic model refresh" if kind == MODEL else JOB_ITEMS[kind]
    label = "semantic_model_refresh" if kind == MODEL else f"fabric_{kind.lower()}"
    def ended(r: dict) -> datetime:
        return datetime.fromisoformat(r["start"]) + timedelta(seconds=r["duration_s"] or 0)
    def show(r: dict) -> Run:
        return Run(r["start"], r["status"], r["duration_s"])
    signals = []
    finished = [r for r in runs if r["status"] != RUNNING]
    usual = [r["duration_s"] for r in finished if r["status"] == SUCCEEDED and r["duration_s"] is not None]
    last = finished[0] if finished else None
    if last and last["status"] in (FAILED, CANCELED):
        recent = now - datetime.fromisoformat(last["start"]) <= timedelta(hours=cfg.window_hours)
        severity = "critical" if recent and last["status"] == FAILED else "warning"
        reason = f": {last['failure'][:300]}" if last["failure"] else ""
        signals.append(Signal(source, "fabric-failed", name, severity, f"{what} {name} {last['status']} at {last['start']}{reason}", url))
    elif last and len(usual) > cfg.min_runs and last["duration_s"] is not None:
        typical = median(usual[1:])
        if last["duration_s"] >= cfg.overrun_factor * typical and last["duration_s"] - typical >= cfg.overrun_minutes * 60:
            signals.append(Signal(source, "ran-long", name, "warning", f"{what} {name} took {last['duration_s'] / 60:.0f} min at "
                                  f"{last['start']}; it usually takes {typical / 60:.0f} min", url))
    starts = [datetime.fromisoformat(r["start"]) for r in runs if r["scheduled"]]     # a manual run says nothing about a schedule
    running = len(finished) < len(runs)
    if len(starts) > cfg.min_runs and not running:
        longest = max((a - b).total_seconds() for a, b in zip(starts, starts[1:]))
        quiet = (now - starts[0]).total_seconds()
        if longest > 0 and quiet >= 1.1 * longest + cfg.grace_minutes * 60:
            signals.append(Signal(source, "overdue", name, "warning", f"{what} {name} last ran on schedule at {starts[0].isoformat()}: "
                                  f"{quiet / 3600:.1f} hours ago, and it has not gone more than {longest / 3600:.1f} hours between runs lately", url))
    period = [r for r in runs if r["status"] == RUNNING or ended(r) >= since]
    if not period:
        if any(s.rule == "overdue" for s in signals):
            return Activity(source, label, name, what, "missing"), signals
        return None, signals
    bad = [r for r in period if r["status"] in (FAILED, CANCELED)]
    newest = next((r for r in period if r["status"] != RUNNING), None)
    state = (RUNNING if running else FAILED if newest and newest["status"] in (FAILED, CANCELED) else "recovered" if bad
             else "slow" if any(s.rule == "ran-long" for s in signals) else "ok")
    return Activity(source, label, name, what, state, [show(r) for r in period],
                    median(usual) if usual else None), signals


class FabricSource:
    kind = "fabric_workspace"

    def __init__(self, name: str, workspace: str, cfg: Config, client: FabricClient):
        self.name, self.workspace, self.cfg, self.client = name, workspace, cfg, client

    def _read(self, item: dict) -> tuple[dict, list[dict] | None, ApiError | None]:
        try:
            if item["type"] == MODEL:
                rows = [refresh_run(r) for r in self.client.refreshes(self.workspace, item["id"])]
            else:
                rows = [job_run(r) for r in self.client.job_runs(self.workspace, item["id"])]
        except ApiError as e:
            return item, None, e
        return item, sorted((r for r in rows if r), key=lambda r: r["start"], reverse=True), None

    def collect(self, since: datetime, now: datetime, memory: dict[str, dict], thorough: bool) -> Result:
        url = f"{PORTAL}/{self.workspace}"
        try:
            items = watchable(self.client.items(self.workspace))
        except (ApiError, ValueError) as e:
            return Result(self.name, self.kind, None, signals=[Signal(self.name, "server-unreachable", self.name, "critical",
                          f"could not read Fabric workspace {self.name}: {e}")], looked={"server-unreachable"}, memory=memory,
                          unread={"workspace": str(e)})
        with ThreadPoolExecutor(PARALLEL) as pool:
            read = list(pool.map(self._read, items))
        result = Result(self.name, self.kind, "Fabric", looked=set(RULES), memory=memory, watched=len(items))
        refused: dict[str, int] = {}
        for item, runs, error in read:
            if error is not None:
                refused[error.code] = refused.get(error.code, 0) + 1
                if error.transient:      # it may well have failed: what was open stays open
                    result.looked = {"server-unreachable"}
                continue
            activity, signals = judge(self.name, item, runs or [], since, now, self.cfg, url)
            if activity:
                result.activities.append(activity)
            result.signals += signals
        if refused:
            total = sum(refused.values())
            codes = ", ".join(f"{n} {code}" for code, n in sorted(refused.items()))
            result.unread["run history"] = (f"{total} of {len(items)} items could not be read ({codes}). A semantic model's refresh "
                                            f"history takes Write permission on it, and some models have none")
        result.activities.sort(key=lambda a: a.runs[0].start if a.runs else "")
        return result
