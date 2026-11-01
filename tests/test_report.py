"""The morning report: run once, with nothing prepared."""
import json

from conftest import job, run_rows, step


def report(estate, now="2026-11-02T08:00:00", *more):
    estate.save()
    doc, code = estate.cli("report", "--format", "json", "--now", now, *more)
    assert code == 0, doc
    return doc


def nightly(estate, status=1, start="2026-11-02T01:00:00"):
    estate.jobs = [job("Nightly", start, status=status)]
    estate.steps = [step("Nightly")]
    estate.history = run_rows("Nightly", 2, start, status=status, steps=[(1, "Load", status, "Execution ID: 77")])


def test_all_clear_says_what_ran_and_works_on_a_first_run(estate):
    nightly(estate)
    doc = report(estate)
    assert doc["verdict"] == "All clear: 1 ran, nothing needs you" and doc["since"] == "2026-11-02T00:00:00"
    [a] = doc["activities"]
    assert (a["name"], a["state"], a["what"], a["kind"]) == ("Nightly", "ok", "SSIS P/Main.dtsx", "sql_agent_job")


def test_jobs_that_did_not_run_today_are_not_listed(estate):
    nightly(estate, start="2026-11-01T01:00:00")
    assert report(estate)["activities"] == []


def test_a_run_that_crossed_midnight_counts(estate):
    estate.jobs = [job("Late", "2026-11-01T23:50:00", duration=3000)]        # HHMMSS: 30 minutes
    estate.history = run_rows("Late", 1, "2026-11-01T23:50:00", duration=3000)
    assert [a["name"] for a in report(estate)["activities"]] == ["Late"]


def test_what_needs_attention_comes_with_the_cause_and_what_to_run(estate):
    nightly(estate, status=0)
    doc = report(estate)
    assert doc["verdict"] == "1 thing needs your attention" and doc["counts"]["failed"] == 1
    a = doc["attention"][0]
    assert a["rule"] == "job-failed" and a["severity"] == "critical" and a["next"] == "ssisbliss executions --env prod --id 77"


def test_the_failed_package_is_explained_by_ssisbliss(estate):
    from test_rules import _failed_execution
    nightly(estate, status=0)
    _failed_execution(estate.catalog(), 77, "Load:Error: The database is read-only.")
    message = report(estate)["attention"][0]["message"]
    assert "Package task Load" in message and "read-only" in message


def test_an_old_failure_stays_open_below_last_nights(estate):
    estate.jobs = [job("Old", "2026-10-29T01:00:00", status=0), job("Nightly", "2026-11-02T01:00:00", status=0)]
    assert [(a["subject"], a["severity"]) for a in report(estate)["attention"]] == [("Nightly", "critical"), ("Old", "warning")]


def test_a_failure_that_a_rerun_fixed_is_recovered_not_attention(estate):
    estate.jobs = [job("Nightly", "2026-11-02T03:00:00")]
    estate.history = (run_rows("Nightly", 2, "2026-11-02T03:00:00") + run_rows("Nightly", 1, "2026-11-02T01:00:00", status=0))
    doc = report(estate)
    assert doc["activities"][0]["state"] == "recovered" and doc["attention"] == []


def test_a_daily_job_that_did_not_run_is_overdue_with_no_memory(estate):
    estate.jobs = [job("Nightly", "2026-10-31T01:00:00", next_run="2026-11-03T01:00:00")]
    estate.history = [r for i in range(5) for r in run_rows("Nightly", i + 1, f"2026-10-{27 + i}T01:00:00")]
    a = report(estate, "2026-11-02T08:00:00")["attention"][0]
    assert a["rule"] == "overdue" and "last started at 2026-10-31T01:00:00" in a["message"]


def test_a_job_that_skips_weekends_is_not_overdue_on_sunday(estate):
    days = ["2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]     # Friday last
    estate.jobs = [job("Weekdays", f"{days[-1]}T01:00:00")]
    estate.history = [r for i, d in enumerate(days) for r in run_rows("Weekdays", i + 1, f"{d}T01:00:00")]
    assert report(estate, "2026-11-01T08:00:00")["attention"] == []


def test_a_job_that_reports_success_but_failed_a_step_is_suspect_not_fine(estate):
    estate.jobs = [job("Archive", "2026-11-02T05:00:00")]
    estate.steps = [step("Archive", 1, "Copy", "PowerShell", "C:\\etl\\archive.ps1", on_fail=3)]
    estate.history = run_rows("Archive", 1, "2026-11-02T05:00:00", steps=[(1, "Copy", 0, "Access denied")])
    doc = report(estate)
    assert doc["activities"][0]["state"] == "suspect" and doc["counts"]["suspect"] == 1 and doc["counts"]["ok"] == 0


def test_a_report_keeps_what_was_acknowledged_apart(estate):
    nightly(estate, status=0)
    estate.check()
    [alert] = estate.cli("alerts")[0]["alerts"]
    estate.cli("ack", str(alert["id"]), "--note", "vendor file late")
    doc = report(estate)
    assert doc["attention"] == [] and doc["acknowledged"][0]["ack_note"] == "vendor file late"
    assert doc["verdict"] == "All clear: 1 ran, nothing needs you"


def test_a_server_that_does_not_answer_is_not_all_clear(estate):
    nightly(estate)
    estate.save()
    (estate.server / "msdb.json").unlink()
    doc, code = estate.cli("report", "--format", "json", "--now", "2026-11-02T08:00:00")
    assert code == 0 and doc["verdict"] == "1 thing needs your attention" and doc["attention"][0]["rule"] == "server-unreachable"
    assert doc["sources"][0]["unread"]


def test_since_widens_the_period(estate):
    nightly(estate, start="2026-11-01T22:00:00")
    assert [a["name"] for a in report(estate, "2026-11-02T08:00:00", "--since", "2026-11-01T18:00:00")["activities"]] == ["Nightly"]


def test_markdown_is_the_default_and_reads_top_down(estate):
    nightly(estate, status=0)
    estate.jobs.append(job("Other", "2026-11-02T02:00:00"))
    estate.history += run_rows("Other", 5, "2026-11-02T02:00:00")
    estate.save()
    text, code = estate.cli("report", "--now", "2026-11-02T08:00:00")
    assert code == 0 and text.startswith("# 1 thing needs your attention")
    assert text.index("## Needs you now") < text.index("## What ran (2)")
    assert "`ssisbliss executions --env prod --id 77`" in text
    assert text.index("| failed |") < text.index("| ok |")


def test_a_report_counts_towards_what_check_remembers(estate):
    nightly(estate)
    report(estate)
    estate.jobs = [job("Nightly", "2026-11-02T01:00:00", enabled=False)]
    estate.check("2026-11-02T09:00:00")
    assert ("job-unscheduled", "Nightly") in estate.open()


def test_a_problem_only_history_shows_is_not_resolved_by_the_next_quick_pass(estate):
    """A check reads history only for what changed: not reading it again must not count as the problem being gone."""
    estate.jobs = [job("Nightly", "2026-11-02T01:00:00")]
    estate.steps = [step("Nightly", 1, "Clean up", "TSQL", "EXEC x", on_fail=3)]
    estate.history = run_rows("Nightly", 1, "2026-11-02T01:00:00", steps=[(1, "Clean up", 0, "Deadlock victim")])
    estate.check("2026-11-02T08:00:00")
    key = ("step-failed-silently", "Nightly / Clean up")
    assert key in estate.open()
    done = estate.check("2026-11-02T08:05:00")
    assert key in estate.open() and done["resolved"] == []
    estate.jobs = [job("Nightly", "2026-11-03T01:00:00")]
    estate.history = run_rows("Nightly", 2, "2026-11-03T01:00:00", steps=[(1, "Clean up", 1, "ok")])
    estate.check("2026-11-03T08:00:00")        # a new clean run clears it
    assert key not in estate.open()


def test_a_quick_pass_still_knows_which_job_a_failed_package_belongs_to(estate):
    from test_rules import _failed_execution
    nightly(estate, status=0)
    _failed_execution(estate.catalog(), 77)
    report(estate)
    estate.check("2026-11-02T08:05:00")          # reads no history, and must not call it a package no job accounts for
    assert list(estate.open()) == [("job-failed", "Nightly")]


def test_the_failure_detail_survives_quick_passes(estate):
    nightly(estate, status=0)
    estate.check("2026-11-02T08:00:00")
    before = estate.open()[("job-failed", "Nightly")]
    estate.check("2026-11-02T08:05:00")
    after = estate.open()[("job-failed", "Nightly")]
    assert after["message"] == before["message"] and after["next"] == before["next"] and after["passes"] == 2


def test_windows_integrated_auth_is_what_an_environment_without_a_user_gets(monkeypatch):
    import sys
    from types import SimpleNamespace

    from ssisbliss.sql import connection_string
    monkeypatch.setitem(sys.modules, "pyodbc", SimpleNamespace(drivers=lambda: ["ODBC Driver 18 for SQL Server"]))
    text = connection_string("SQL01", None, True, database="msdb")
    assert "Trusted_Connection=yes" in text and "UID=" not in text and "PWD=" not in text


def test_json_report_keeps_the_contract(estate):
    nightly(estate)
    doc = report(estate)
    assert doc["ok"] is True and json.dumps(doc)
