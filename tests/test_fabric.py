"""Fabric: tokens from the Azure CLI, the two APIs, and what their answers mean."""
import io
import json
import stat
import urllib.error
from datetime import datetime, timezone

import pytest

from loadwatch import discover
from loadwatch.activity import Activity, Result, Run, Signal
from loadwatch.config import Config
from loadwatch.report import build
from loadwatch.sources import fabric


def fake_az(tmp_path, monkeypatch, script):
    az = tmp_path / "az"
    az.write_text(f"#!/bin/sh\n{script}\n")
    az.chmod(az.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")


def test_a_token_comes_from_the_azure_cli_for_the_resource_asked(tmp_path, monkeypatch):
    doc = json.dumps({"accessToken": "secret-value", "expiresOn": "2026-11-02 09:00:00"})
    fake_az(tmp_path, monkeypatch, f'echo "$@" > {tmp_path}/args\ncat <<JSON\n{doc}\nJSON')
    token = fabric.az_token(fabric.FABRIC)
    assert token.value == "secret-value" and token.expires_on == "2026-11-02 09:00:00"
    assert (tmp_path / "args").read_text().strip() == f"account get-access-token --resource {fabric.FABRIC} --output json"


def test_a_token_never_shows_in_a_log(tmp_path, monkeypatch):
    assert "secret" not in repr(fabric.Token("secret-value", "later")) and "secret" not in f"{fabric.Token('secret-value', 'later')}"


def test_not_logged_in_says_what_to_do(tmp_path, monkeypatch):
    fake_az(tmp_path, monkeypatch, 'echo "Please run az login" >&2\nexit 1')
    with pytest.raises(ValueError, match="run `az login`"):
        fabric.az_token(fabric.POWER_BI)


def test_no_azure_cli_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ValueError, match="install it"):
        fabric.az_token(fabric.FABRIC)


def test_the_services_status_words_become_ours():
    assert fabric.JOB_STATUS["Completed"] == fabric.REFRESH_STATUS["Completed"] == "succeeded"
    assert fabric.JOB_STATUS["Failed"] == fabric.REFRESH_STATUS["Failed"] == "failed"
    assert fabric.REFRESH_STATUS["Unknown"] == "running"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def client(*answers, sleeps=None):
    """A client whose calls get `answers` in order: a dict is a 200, a (status, body, headers) tuple an HTTP error."""
    queue, seen = list(answers), []

    def opener(request, timeout=0):
        seen.append(request)
        answer = queue.pop(0)
        if isinstance(answer, tuple):
            status, body, headers = answer
            raise urllib.error.HTTPError(request.full_url, status, "x", headers, io.BytesIO(json.dumps(body).encode()))
        return Response(json.dumps(answer).encode())
    c = fabric.FabricClient(lambda r: fabric.Token(f"token-for-{r}", "later"), opener, (sleeps if sleeps is not None else []).append)
    c.seen = seen
    return c


def test_every_page_is_read_and_the_token_never_follows_a_url_out_of_a_response():
    c = client({"value": [{"id": 1}], "continuationToken": "a b", "continuationUri": "https://evil.example/next"}, {"value": [{"id": 2}]})
    assert [w["id"] for w in c.workspaces()] == [1, 2]
    assert [r.full_url for r in c.seen] == [f"{fabric.FABRIC}/v1/workspaces", f"{fabric.FABRIC}/v1/workspaces?continuationToken=a%20b"]


def test_each_api_gets_its_own_token_once():
    c = client({"value": []}, {"value": []})
    c.job_runs("w", "i")
    c.refreshes("w", "m")
    assert c.seen[0].get_header("Authorization") == f"Bearer token-for-{fabric.FABRIC}"
    assert c.seen[1].get_header("Authorization") == f"Bearer token-for-{fabric.POWER_BI}"
    assert c.seen[1].full_url.startswith("https://api.powerbi.com/v1.0/myorg/groups/w/datasets/m/refreshes")


def test_it_backs_off_when_told_to():
    sleeps = []
    c = client((429, {"errorCode": "RequestBlocked"}, {"Retry-After": "7"}), {"value": []}, sleeps=sleeps)
    assert c.items("w") == [] and sleeps == [7.0]


def test_a_refusal_is_an_error_with_its_code():
    with pytest.raises(fabric.ApiError) as e:
        client((403, {"errorCode": "InsufficientPrivileges"}, {})).items("w")
    assert e.value.code == "InsufficientPrivileges" and not e.value.transient


def test_a_powerbi_error_has_its_code_too():
    with pytest.raises(fabric.ApiError) as e:
        client((401, {"error": {"code": "GroupNotAccessible"}}, {})).refreshes("w", "m")
    assert e.value.code == "GroupNotAccessible"


CFG = Config()
NOW = datetime(2026, 11, 2, 8, 0, 0)
SINCE = datetime(2026, 11, 2)
ITEM = {"id": "i", "displayName": "Sales", "type": "SemanticModel"}
PIPE = {"id": "p", "displayName": "Load", "type": "DataPipeline"}


def run(start, status="succeeded", took=600, scheduled=True, failure=""):
    return {"start": start, "status": status, "duration_s": took, "scheduled": scheduled, "failure": failure}


def judged(runs, item=ITEM):
    return fabric.judge("Sales", item, runs, SINCE, NOW, CFG, "https://app/x")


def test_a_service_time_lands_on_the_local_clock():
    expect = datetime(2026, 11, 2, 6, 35, tzinfo=timezone.utc).astimezone().replace(tzinfo=None).isoformat()[:19]
    assert fabric.local("2026-11-02T06:35:00.7812154") == expect == fabric.local("2026-11-02T06:35:00Z")


def test_a_deduped_job_never_ran():
    assert fabric.job_run({"status": "Deduped", "startTimeUtc": "2026-11-02T06:35:00"}) is None


def test_a_job_failure_keeps_its_reason():
    r = fabric.job_run({"status": "Failed", "invokeType": "Scheduled", "startTimeUtc": "2026-11-02T06:35:00", "endTimeUtc": "2026-11-02T06:36:00",
                        "failureReason": {"errorCode": "JobFailed", "message": "Activity Copy failed"}})
    assert (r["status"], r["duration_s"], r["scheduled"], r["failure"]) == ("failed", 60, True, "JobFailed Activity Copy failed")


def test_a_refresh_failure_keeps_its_reason():
    r = fabric.refresh_run({"status": "Failed", "refreshType": "Scheduled", "startTime": "2026-11-02T06:00:00Z", "endTime": "2026-11-02T06:01:00Z",
                            "serviceExceptionJson": '{"errorCode":"ModelRefreshFailed_CredentialsNotSpecified"}'})
    assert r["failure"] == "ModelRefreshFailed_CredentialsNotSpecified"
    running = fabric.refresh_run({"status": "Unknown", "startTime": "2026-11-02T06:00:00Z"})
    assert running["status"] == "running" and running["duration_s"] is None


def test_a_failed_refresh_tonight_needs_you_now_with_its_reason():
    activity, [s] = judged([run("2026-11-02T05:00:00", "failed", failure="CredentialsNotSpecified")])
    assert activity.state == "failed" and activity.kind == "semantic_model_refresh"
    assert (s.rule, s.severity) == ("fabric-failed", "critical") and "CredentialsNotSpecified" in s.message and s.next == "https://app/x"


def test_an_old_failure_ranks_below_tonights_and_a_cancelled_run_is_a_warning():
    assert judged([run("2026-10-20T05:00:00", "failed")])[1][0].severity == "warning"
    assert judged([run("2026-11-02T05:00:00", "canceled")])[1][0].severity == "warning"


def test_a_rerun_that_worked_is_recovered():
    activity, signals = judged([run("2026-11-02T06:00:00"), run("2026-11-02T05:00:00", "failed")])
    assert activity.state == "recovered" and signals == []


def test_nothing_in_the_period_is_not_listed():
    assert judged([run("2026-10-30T05:00:00")]) == (None, [])


def test_a_refresh_far_longer_than_usual_is_slow():
    runs = [run("2026-11-02T05:00:00", took=7200)] + [run(f"2026-10-2{i}T05:00:00", took=1800) for i in range(5)]
    activity, [s] = judged(runs)
    assert activity.state == "slow" and s.rule == "ran-long" and "usually takes 30 min" in s.message


def test_a_scheduled_item_that_went_quiet_is_overdue_and_shows_as_not_run():
    runs = [run(f"2026-10-{d}T05:00:00") for d in (29, 28, 27, 26, 25, 24)]
    activity, [s] = judged(runs)
    assert s.rule == "overdue" and activity.state == "missing" and activity.runs == []


def test_manual_runs_say_nothing_about_a_schedule():
    assert judged([run(f"2026-10-{d}T05:00:00", scheduled=False) for d in (29, 28, 27, 26, 25, 24)]) == (None, [])


class Client:
    """What a FabricSource needs."""

    def __init__(self, items, runs):
        self._items, self._runs = items, runs

    def items(self, workspace):
        if isinstance(self._items, Exception):
            raise self._items
        return self._items

    def _answer(self, item):
        answer = self._runs[item]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def job_runs(self, workspace, item):
        return self._answer(item)

    def refreshes(self, workspace, model):
        return self._answer(model)


def collect(items, runs):
    return fabric.FabricSource("Sales", "w", CFG, Client(items, runs)).collect(SINCE, NOW, {}, True)


def test_only_what_runs_is_read():
    r = collect([ITEM, PIPE, {"id": "x", "displayName": "A report", "type": "Report"}], {"i": [], "p": []})
    assert r.watched == 2 and r.activities == [] and r.signals == [] and r.unread == {}


def test_a_refused_item_is_not_read_not_failed():
    r = collect([ITEM, PIPE], {"i": fabric.ApiError(401, "GroupNotAccessible"), "p": []})
    assert r.signals == [] and "1 of 2 items could not be read (1 GroupNotAccessible)" in r.unread["run history"]
    assert "fabric-failed" in r.looked


def test_a_throttled_read_clears_no_alert():
    assert "fabric-failed" not in collect([ITEM], {"i": fabric.ApiError(429, "RequestBlocked")}).looked


def test_a_workspace_that_cannot_be_read_says_so_and_clears_nothing():
    r = collect(fabric.ApiError(403, "InsufficientPrivileges"), {})
    assert [s.rule for s in r.signals] == ["server-unreachable"] and r.looked == {"server-unreachable"}


def test_a_missing_login_is_reported_on_the_workspace_too():
    assert collect(ValueError("run `az login`"), {}).signals[0].severity == "critical"


def test_discovery_skips_admin_workspaces_and_says_which_have_nothing_running():
    class Listing:
        def workspaces(self):
            return [{"id": "a", "displayName": "Sales", "type": "Workspace"}, {"id": "b", "displayName": "Empty", "type": "Workspace"},
                    {"id": "c", "displayName": "Audit", "type": "AdminWorkspace"}]

        def items(self, workspace):
            return [ITEM, PIPE] if workspace == "a" else [{"id": "x", "displayName": "R", "type": "Report"}]
    found, notes = discover.find_fabric(Listing())
    assert notes == [] and [(t.name, t.can, t.size) for t in found] == [("Sales", ["scheduled items", "semantic model refreshes"], 2), ("Empty", [], 0)]
    assert found[1].why_not == "nothing in it runs"


def test_a_new_kind_of_source_needs_no_change_in_the_report():
    """What a Fabric source will hand over, in the one report."""
    refresh = Activity("sales-workspace", "semantic_model_refresh", "Sales model", "Power BI refresh", "failed",
                       [Run("2026-11-02T05:00:00", "failed", 120)], usual_s=600)
    r = Result("sales-workspace", "fabric", "Sales", [refresh], [Signal("sales-workspace", "refresh-failed", "Sales model", "critical", "x")],
               {"refresh-failed"}, {}, 1)
    doc = build([r], [], datetime(2026, 11, 2), datetime(2026, 11, 2, 8))
    assert doc["activities"][0]["kind"] == "semantic_model_refresh" and doc["counts"]["failed"] == 1


def test_a_refresh_running_far_longer_than_usual_is_overrunning():
    runs = [run("2026-11-02T02:00:00", "running", took=None)] + [run(f"2026-10-2{i}T02:00:00", took=1200) for i in range(5)]
    activity, [s] = judged(runs)
    assert s.rule == "overrunning" and activity.state == "running" and "has been running for 360 min" in s.message and "usually takes 20 min" in s.message


def test_a_refresh_running_for_about_as_long_as_usual_is_fine():
    runs = [run("2026-11-02T07:50:00", "running", took=None)] + [run(f"2026-10-2{i}T02:00:00", took=1200) for i in range(5)]
    assert judged(runs)[1] == []
