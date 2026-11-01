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


def test_start_here_is_what_blocks_others_not_what_failed_first(estate):
    estate.jobs = [job("Rates", "2026-11-02T00:10:00", status=0), job("1 A", "2026-11-02T00:30:00"), job("2 B", "2026-11-02T00:45:00", status=0),
                   job("3 C", "2026-11-01T01:00:00", schedules=0)]
    estate.steps = [step("1 A", 1, "Next", "TSQL", "EXEC msdb.dbo.sp_start_job @job_name = N'2 B';"),
                    step("2 B", 1, "Next", "TSQL", "EXEC msdb.dbo.sp_start_job @job_name = N'3 C';")]
    doc = report(estate)
    assert doc["start_here"] == "2 B" and doc["start_because"] == "3 C did not run because of it"
    assert [a["subject"] for a in doc["attention"]] == ["2 B", "Rates"] and doc["attention"][0]["blocks"] == ["3 C"]


def test_a_new_failure_comes_before_one_that_has_been_failing(estate):
    days = ["2026-11-02", "2026-11-01", "2026-10-31", "2026-10-30", "2026-10-29"]
    estate.jobs = [job("Legacy", "2026-11-02T00:10:00", status=0), job("Sales", "2026-11-02T01:00:00", status=0)]
    estate.history = [r for i, d in enumerate(days) for r in run_rows("Legacy", 10 - i, f"{d}T00:10:00", status=0)]
    estate.history += run_rows("Sales", 3, "2026-11-02T01:00:00", status=0) + run_rows("Sales", 2, "2026-11-01T01:00:00")
    doc = report(estate)
    assert doc["start_here"] == "Sales" and "first failure after 1 good runs" in doc["attention"][0]["message"]
    legacy = doc["attention"][1]
    assert legacy["in_a_row"] == 5 and "Failed 5 runs in a row" in legacy["message"]


def test_tonights_warning_comes_before_an_old_failure(estate):
    estate.jobs = [job("Old", "2026-10-02T02:00:00", status=0), job("Archive", "2026-11-02T03:00:00")]
    estate.steps = [step("Archive", 1, "Copy", "PowerShell", "x.ps1", on_fail=3)]
    estate.history = run_rows("Archive", 1, "2026-11-02T03:00:00", steps=[(1, "Copy", 0, "Access denied")])
    assert report(estate)["start_here"] == "Archive / Copy"


def test_a_persons_test_run_comes_last_and_says_so(estate):
    from datetime import datetime, timedelta

    from test_rules import _failed_execution
    now = datetime.now().replace(microsecond=0)
    estate.jobs = [job("Daily", (now - timedelta(hours=2)).isoformat(), status=0)]
    _failed_execution(estate.catalog(), 88)
    cat = estate.catalog()
    cat.execution_rows[0]["caller_name"] = "LAB\\dave"
    cat.save()
    doc = report(estate, now.isoformat())
    assert doc["start_here"] == "Daily" and doc["attention"][1]["person"] == "LAB\\dave" and "probably a test" in doc["attention"][1]["message"]


def test_a_server_that_does_not_answer_comes_first():
    from datetime import datetime

    from loadwatch.activity import priority
    now = datetime(2026, 11, 2, 8)
    alerts = [{"rule": "job-failed", "severity": "critical", "at": "2026-11-02T01:00:00", "opened_at": "x", "subject": "a"},
              {"rule": "server-unreachable", "severity": "critical", "opened_at": "x", "subject": "b"}]
    assert sorted(alerts, key=lambda a: priority(a, now))[0]["subject"] == "b"


def test_the_report_opens_with_where_to_start(estate):
    nightly(estate, status=0)
    estate.save()
    text, _ = estate.cli("report", "--now", "2026-11-02T08:00:00")
    assert text.split("\n")[6].startswith("**Start here: Nightly**") and "Everything that needs attention: Nightly." in text


def test_anything_that_might_be_a_service_account_is_not_called_a_person():
    from loadwatch.rules import _SERVICE
    for account in ("CORP\\svc_etl", "CORP\\sql-agent", "NT SERVICE\\SQLAgent$PROD", "CORP\\SQL01$", "CORP\\etl.batch", "sa", "agent"):
        assert _SERVICE.search(account), account
    assert not _SERVICE.search("CORP\\dave") and not _SERVICE.search("LAB\\anna.berg")


def test_the_report_says_what_it_looked_at(estate):
    nightly(estate)
    estate.save()
    doc = report(estate)
    assert doc["covered"]["sql_servers"] == ["prod"] and doc["covered"]["fabric"] == "Fabric is not watched: loadwatch.toml lists what to watch"
    text, _ = estate.cli("report", "--now", "2026-11-02T08:00:00")
    assert "Looked at 1 SQL Server (prod), and Fabric is not watched" in text and "This is the whole picture" in text


def test_help_says_the_report_is_the_whole_answer():
    from loadwatch import cli
    text = cli.parser().format_help()
    assert "It is the whole answer" in text and "start here" in text


def test_the_reason_to_start_is_never_new_for_something_old(estate):
    estate.jobs = [job("Stale", "2026-10-29T15:44:00", status=0)]
    estate.history = run_rows("Stale", 2, "2026-10-29T15:44:00", status=0) + run_rows("Stale", 1, "2026-10-28T15:44:00")
    doc = report(estate)
    assert doc["start_here"] == "Stale" and doc["start_because"] == "nothing is new, and this is the first of what is still open, from 2026-10-29"
    assert doc["attention"][0]["in_a_row"] == 1


def test_a_failure_from_last_night_is_new(estate):
    estate.jobs = [job("Fresh", "2026-11-02T01:00:00", status=0)]
    estate.history = run_rows("Fresh", 2, "2026-11-02T01:00:00", status=0) + run_rows("Fresh", 1, "2026-11-01T01:00:00")
    assert report(estate)["start_because"] == "it is new"


def overrun_world(estate):
    estate.config('[[expect]]\nenv = "prod"\njob = "3 C"\nby = "06:00"\n')
    estate.jobs = [job("1 A", "2026-11-02T00:30:00"), job("2 B", "2026-11-01T01:00:00", running_since="2026-11-02T01:00:00"),
                   job("3 C", "2026-11-01T02:00:00", schedules=0)]
    estate.steps = [step("1 A", 1, "Next", "TSQL", "EXEC msdb.dbo.sp_start_job @job_name = N'2 B';"),
                    step("2 B", 1, "Next", "TSQL", "EXEC msdb.dbo.sp_start_job @job_name = N'3 C';")]
    estate.history = [r for i in range(5) for r in run_rows("2 B", 9 - i, f"2026-10-2{i}T01:00:00", duration=1000)]


def test_a_job_that_overruns_comes_before_the_deadline_it_makes_others_miss(estate):
    overrun_world(estate)
    doc = report(estate)
    assert doc["start_here"] == "2 B" and doc["start_because"] == "3 C cannot start until it finishes"
    first, second = doc["attention"]
    assert first["rule"] == "overrunning" and first["blocks"] == ["3 C"] and "3 C cannot start until it finishes" in first["message"]
    assert second["rule"] == "deadline-missed" and second["caused_by"] == "2 B" and "It is waiting for 2 B" in second["message"]


def test_a_deadline_missed_with_no_cause_keeps_its_rank(estate):
    estate.config('[[expect]]\nenv = "prod"\njob = "Alone"\nby = "06:00"\n')
    estate.jobs = [job("Alone", "2026-11-01T01:00:00"), job("Other", "2026-11-02T01:00:00")]
    doc = report(estate)
    assert doc["start_here"] == "Alone" and "caused_by" not in doc["attention"][0]


def test_a_job_that_fails_on_and_off_is_flaky_even_on_a_good_night(estate):
    days = ["2026-11-02", "2026-11-01", "2026-10-31", "2026-10-30", "2026-10-29", "2026-10-28", "2026-10-27"]
    estate.jobs = [job("Wobbly", "2026-11-02T01:00:00")]
    estate.history = [r for i, d in enumerate(days) for r in run_rows("Wobbly", 20 - i, f"{d}T01:00:00", status=0 if i % 2 else 1)]
    a = report(estate)["attention"][0]
    assert a["rule"] == "flaky" and "failed 3 of its last 7 runs" in a["message"]


def test_one_bad_night_is_not_flaky(estate):
    estate.jobs = [job("Fine", "2026-11-02T01:00:00")]
    estate.history = run_rows("Fine", 3, "2026-11-02T01:00:00") + run_rows("Fine", 2, "2026-11-01T01:00:00", status=0) + run_rows("Fine", 1, "2026-10-31T01:00:00")
    assert report(estate)["attention"] == []


def many_days(name, last="2026-11-01"):
    days = ["2026-11-01", "2026-10-31", "2026-10-30", "2026-10-29", "2026-10-28", "2026-10-27"]
    return [r for i, d in enumerate(days) for r in run_rows(name, 20 - i, f"{d}T01:00:00")]


def test_when_most_jobs_stop_starting_the_finding_is_the_server(estate):
    names = ("A", "B", "C", "D")
    estate.jobs = [job(n, "2026-11-01T01:00:00") for n in names]
    estate.history = [r for n in names for r in many_days(n)]
    doc = report(estate, "2026-11-03T08:00:00")
    assert doc["attention"][0]["rule"] == "nothing-started" and "Check that SQL Server Agent is running" in doc["attention"][0]["message"]
    assert doc["start_because"].startswith("jobs have stopped starting") and all(a["caused_by"] for a in doc["attention"][1:])


def test_one_overdue_job_is_only_that_job(estate):
    estate.jobs = [job("A", "2026-11-01T01:00:00"), job("B", "2026-11-03T01:00:00"), job("C", "2026-11-03T01:00:00")]
    estate.history = many_days("A")
    assert [a["rule"] for a in report(estate, "2026-11-03T08:00:00")["attention"]] == ["overdue"]


def failing_with(estate, names, message):
    estate.jobs = [job(n, "2026-11-02T01:00:00", status=0) for n in names]
    estate.history = [r for n in names for r in run_rows(n, 5, "2026-11-02T01:00:00", status=0, steps=[(1, "Run", 0, message)])]


def test_the_same_error_in_several_jobs_is_one_cause(estate):
    failing_with(estate, ["A", "B", "C"], "Executed as user: LAB\\svc. The transaction log for database DW is full. Error 9002.")
    doc = report(estate)
    first = doc["attention"][0]
    assert first["rule"] == "shared-cause" and doc["start_because"] == "one fix may clear all of them"
    assert "The same error in 3 jobs" in first["message"] and "(A, B, C)" in first["message"]
    assert all(a["caused_by"] == first["subject"] for a in doc["attention"][1:])


def test_different_errors_are_different_causes(estate):
    failing_with(estate, ["A", "B"], "The transaction log is full")
    assert all(a["rule"] == "job-failed" for a in report(estate)["attention"])


def test_the_generic_message_is_not_a_shared_cause(estate):
    failing_with(estate, ["A", "B", "C"], "The job failed.")
    assert all(a["rule"] == "job-failed" for a in report(estate)["attention"])


def deployed_world(estate, deployed_at_lsn):
    from ssisbliss.ssis.fake import dtsx, ispac
    from test_rules import _failed_execution
    cat = estate.catalog()
    cat.next_lsn = deployed_at_lsn
    cat.add_project("F", "P", ispac("P", {"Main.dtsx": dtsx("Main.dtsx")}), deployed_by="LAB\\bob")
    cat.save()
    _failed_execution(cat, 71)
    cat = estate.catalog()
    cat.execution_rows[0]["caller_name"] = "NT SERVICE\\SQLAgent"
    cat.save()
    return cat


def test_a_failure_says_who_deployed_just_before_it(estate):
    from datetime import datetime, timedelta
    now = datetime.now().replace(microsecond=0)
    run = now - timedelta(hours=1)
    ticks = int((run - datetime(2026, 10, 1)).total_seconds() // 3600) - 3
    deployed_world(estate, ticks - 1)
    estate.jobs = [job("Silver", run.isoformat(), status=0)]
    estate.history = run_rows("Silver", 5, run.isoformat(), status=0, steps=[(1, "Run", 0, "Failed. Execution ID: 71")])
    a = report(estate, now.isoformat())["attention"][0]
    assert a["deployed_by"] == "LAB\\bob" and "LAB\\bob deployed P at" in a["message"]
    assert report(estate, now.isoformat())["start_because"] == "LAB\\bob deployed it just before it failed"


def test_an_old_deploy_is_not_blamed(estate):
    from datetime import datetime, timedelta
    now = datetime.now().replace(microsecond=0)
    run = now - timedelta(hours=1)
    deployed_world(estate, 5)     # weeks ago
    estate.jobs = [job("Silver", run.isoformat(), status=0)]
    estate.history = run_rows("Silver", 5, run.isoformat(), status=0, steps=[(1, "Run", 0, "Failed. Execution ID: 71")])
    a = report(estate, now.isoformat())["attention"][0]
    assert "deployed_by" not in a and " deployed " not in a["message"]
