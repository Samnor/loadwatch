"""Alerts over time: told once, reminded, acknowledged, resolved; and silence is never taken for good news."""
import json

from conftest import job


def failed(estate, status=0):
    estate.jobs = [job("Nightly", "2026-11-02T01:00:00", status=status)]


def test_an_alert_is_told_once_then_again_only_when_it_ends(estate):
    failed(estate)
    assert [a["rule"] for a in estate.check("2026-11-02T08:00:00")["new"]] == ["job-failed"]
    assert estate.check("2026-11-02T08:05:00")["new"] == []
    failed(estate, status=1)
    estate.jobs[0]["last_start"] = "2026-11-02T08:06:00"
    done = estate.check("2026-11-02T08:10:00")
    assert [a["rule"] for a in done["resolved"]] == ["job-failed"] and done["summary"] == "nothing open"
    assert estate.check("2026-11-02T08:15:00")["resolved"] == []


def test_it_comes_back_unless_someone_owns_it(estate):
    failed(estate)
    estate.check("2026-11-02T08:00:00")
    assert len(estate.check("2026-11-02T20:00:00")["new"]) == 1       # 12 hours and nobody acted
    [alert] = estate.cli("alerts")[0]["alerts"]
    doc, code = estate.cli("ack", str(alert["id"]), "--note", "known: vendor file late")
    assert code == 0 and doc["acknowledged"] == alert["id"]
    assert estate.check("2026-11-03T20:00:00")["new"] == []
    assert estate.open()[("job-failed", "Nightly")]["ack_note"] == "known: vendor file late"


def test_a_server_that_does_not_answer_resolves_nothing(estate):
    failed(estate)
    estate.check("2026-11-02T08:00:00")
    (estate.server / "msdb.json").rename(estate.server / "gone.json")
    doc, code = estate.cli("check", "--now", "2026-11-02T08:05:00")
    assert code == 0 and doc["environments"][0]["unread"]
    alerts = estate.open()
    assert ("server-unreachable", "prod") in alerts and ("job-failed", "Nightly") in alerts
    (estate.server / "gone.json").rename(estate.server / "msdb.json")
    failed(estate)
    estate.check("2026-11-02T08:10:00")
    assert list(estate.open()) == [("job-failed", "Nightly")]


def test_a_part_that_cannot_be_read_is_a_blind_spot_and_keeps_its_alerts(estate, monkeypatch):
    from loadwatch import observe
    real = observe.ask

    def no_catalog(target, *command):
        return {"ok": False, "error": "login cannot read SSISDB"} if command[0] == "executions" else real(target, *command)
    from ssisbliss.ssis.fake import FakeCatalog  # noqa: F401
    estate.jobs = [job("Starter", "2026-11-02T01:00:00")]
    monkeypatch.setattr(observe, "ask", no_catalog)
    estate.check()
    a = estate.open()[("blind-spot", "executions")]
    assert "nothing is known about it" in a["message"] and "SSISDB" in a["message"]


def test_a_webhook_that_fails_is_tried_again(estate, monkeypatch):
    import urllib.request
    estate.config('heartbeat = "https://beat.example/x"')
    monkeypatch.setenv("LOADWATCH_WEBHOOK", "https://hook.example/x")
    calls, down = [], [True]

    class Ok:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def post(request, timeout=0):
        calls.append(request)
        if down[0] and "hook" in request.full_url:
            raise OSError("no route")
        return Ok()
    monkeypatch.setattr(urllib.request, "urlopen", post)
    failed(estate)
    doc = estate.check("2026-11-02T08:00:00")
    assert doc["problems"] == ["webhook: no route"]
    down[0] = False
    doc = estate.check("2026-11-02T08:05:00")
    assert "problems" not in doc and len(doc["new"]) == 1
    sent = [json.loads(c.data)["text"] for c in calls if c.data]
    assert "CRITICAL [prod] Nightly failed" in sent[-1]
    assert any(c.full_url == "https://beat.example/x" for c in calls)


def test_status_says_when_each_environment_was_last_seen(estate):
    failed(estate)
    estate.check("2026-11-02T08:00:00")
    doc, _ = estate.cli("status")
    assert doc["summary"] == "1 critical open" and doc["environments"][0]["at"] == "2026-11-02T08:00:00"


def test_usage_and_config_errors_keep_the_contract(estate):
    doc, code = estate.cli("chekc")
    assert code == 5 and doc["error_kind"] == "usage" and "check" in doc["error"]
    from loadwatch import cli
    doc, code = cli.run(["--config", "/nowhere.toml", "status"])
    assert code == 2 and doc["error_kind"] == "refused" and "loadwatch.toml" in doc["error"]
