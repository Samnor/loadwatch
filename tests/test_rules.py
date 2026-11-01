"""Each rule on a fake estate, through `loadwatch check`."""
from datetime import datetime, timedelta

from conftest import job, run_rows, step


def test_a_quiet_night_is_quiet(estate):
    estate.jobs = [job("Nightly", "2026-11-02T01:00:00")]
    estate.steps = [step("Nightly")]
    doc = estate.check()
    assert doc["summary"] == "nothing open" and doc["ok"] and doc["environments"][0]["jobs"] == 2


def test_a_failed_job_names_the_step_and_what_to_run(estate):
    estate.jobs = [job("Nightly", "2026-11-02T01:00:00", status=0)]
    estate.steps = [step("Nightly", 1, "Fetch prices", "CmdExec", "python C:\\etl\\fetch.py")]
    estate.history = run_rows("Nightly", 1, "2026-11-02T01:00:00", status=0, steps=[(1, "Fetch prices", 0, "The step failed. Exit code 2")])
    estate.check()
    a = estate.open()[("job-failed", "Nightly")]
    assert a["severity"] == "critical" and "step 1 (Fetch prices)" in a["message"] and "Exit code 2" in a["message"]
    assert "A CmdExec step" in a["message"]


def test_a_deadline_is_missed_by_a_job_that_did_not_succeed_in_time(estate):
    estate.config('[[expect]]\nenv = "prod"\njob = "Nightly"\nby = "06:00"\n')
    estate.jobs = [job("Nightly", "2026-11-01T01:00:00")]        # yesterday's success
    estate.check("2026-11-02T05:59:00")
    assert not estate.open()                                       # not due yet
    estate.check("2026-11-02T06:01:00")
    assert "should have succeeded by 06:00" in estate.open()[("deadline-missed", "Nightly")]["message"]
    estate.jobs = [job("Nightly", "2026-11-02T06:30:00")]        # late, but it came
    estate.check("2026-11-02T07:00:00")
    assert not estate.open()


def test_a_deadline_only_counts_on_its_days(estate):
    estate.config('[[expect]]\nenv = "prod"\njob = "Nightly"\nby = "06:00"\ndays = ["mon"]\n')   # 2026-11-02 is a Monday
    estate.jobs = [job("Nightly", "2026-11-02T01:00:00")]
    estate.check("2026-11-02T07:00:00")
    estate.jobs = [job("Nightly", "2026-11-02T01:00:00")]
    estate.check("2026-11-08T07:00:00")           # the Sunday after: the Monday deadline was met, no newer one has passed
    assert not estate.open()


def test_an_expected_job_that_does_not_exist(estate):
    estate.config('[[expect]]\nenv = "prod"\njob = "Renamed"\nby = "06:00"\n')
    estate.check()
    assert "no such job" in estate.open()[("deadline-missed", "Renamed")]["message"]


def test_a_schedule_that_passes_without_a_run_only_shows_over_time(estate):
    estate.jobs = [job("Nightly", "2026-11-01T01:00:00", next_run="2026-11-02T01:00:00")]
    estate.check("2026-11-02T00:30:00")
    assert not estate.open()
    estate.jobs = [job("Nightly", "2026-11-01T01:00:00", next_run="2026-11-03T01:00:00")]    # next_run moved on, nothing ran
    estate.check("2026-11-02T02:00:00")
    assert "did not start" in estate.open()[("missed-schedule", "Nightly")]["message"]
    estate.check("2026-11-02T02:05:00")           # stays: the next pass's next_run says nothing about the missed one
    assert ("missed-schedule", "Nightly") in estate.open()
    estate.jobs = [job("Nightly", "2026-11-03T01:00:00", next_run="2026-11-04T01:00:00")]
    estate.check("2026-11-03T02:00:00")
    assert not estate.open()


def test_a_job_someone_turned_off(estate):
    estate.jobs = [job("Nightly", "2026-11-01T01:00:00")]
    estate.check()
    estate.jobs = [job("Nightly", "2026-11-01T01:00:00", enabled=False)]
    estate.check("2026-11-02T09:00:00")
    assert "was disabled" in estate.open()[("job-unscheduled", "Nightly")]["message"]
    estate.jobs = [job("Nightly", "2026-11-01T01:00:00")]
    estate.check("2026-11-02T10:00:00")
    assert not estate.open()


def test_a_job_never_scheduled_is_not_a_finding(estate):
    estate.jobs = [job("Run by hand", schedules=0)]      # started by a chain or a person
    estate.check()
    estate.check("2026-11-02T09:00:00")
    assert not estate.open()


def test_running_far_longer_than_usual(estate):
    estate.jobs = [job("Nightly", "2026-11-01T01:00:00", running_since="2026-11-02T01:00:00")]
    estate.history = [r for i in range(4) for r in run_rows("Nightly", i + 1, f"2026-10-2{i}T01:00:00", duration=1000)]   # 10m00s
    estate.check("2026-11-02T01:15:00")
    assert not estate.open()
    estate.check("2026-11-02T01:45:00")
    assert "usually takes 10 min" in estate.open()[("overrunning", "Nightly")]["message"]


def test_a_job_that_succeeds_with_a_failed_step(estate):
    estate.jobs = [job("Nightly", "2026-11-02T01:00:00")]
    estate.steps = [step("Nightly", 1, "Clean up", "TSQL", "EXEC x", on_fail=3), step("Nightly", 2, "Load")]
    estate.history = run_rows("Nightly", 1, "2026-11-02T01:00:00", steps=[(1, "Clean up", 0, "Deadlock victim"), (2, "Load", 1, "ok")])
    estate.check()
    a = estate.open()[("step-failed-silently", "Nightly / Clean up")]
    assert "reports success" in a["message"] and "Deadlock victim" in a["message"]


def test_a_script_that_exits_0_with_a_traceback(estate):
    estate.jobs = [job("Fetch", "2026-11-02T01:00:00")]
    estate.steps = [step("Fetch", 1, "Fetch prices", "CmdExec", "python fetch.py")]
    estate.history = run_rows("Fetch", 1, "2026-11-02T01:00:00", steps=[(1, "Fetch prices", 1, "Traceback (most recent call last): ...")])
    estate.check()
    assert "exited 0" in estate.open()[("script-output-error", "Fetch / Fetch prices")]["message"]


def _failed_execution(cat, execution_id, job_message="boom"):
    start = (datetime.now() - timedelta(hours=1)).isoformat()[:19]
    cat.execution_rows = [{"execution_id": execution_id, "folder_name": "F", "project_name": "P", "package_name": "Main.dtsx", "status": 4,
                           "environment_folder_name": None, "environment_name": None, "caller_name": "agent", "executed_as_name": "agent",
                           "start_time": start, "end_time": start, "duration_s": 1, "server_name": "S"}]
    cat.event_rows = {execution_id: [{"event_message_id": 1, "message_time": "", "message_type": 120, "message_source_type": 40,
                                      "subcomponent_name": None, "execution_path": "", "message_source_name": "Load",
                                      "package_name": "Main.dtsx", "package_path": "\\Package\\Load", "message": job_message}]}
    cat.save()


def test_a_package_that_failed_behind_a_job_that_succeeded(estate):
    """catalog.start_execution without SYNCHRONIZED: the job only started it."""
    estate.jobs = [job("Starter", "2026-11-02T01:00:00")]
    estate.steps = [step("Starter", 1, "Start", "TSQL", "EXEC SSISDB.catalog.start_execution @id")]
    _failed_execution(estate.catalog(), 77)
    estate.check()
    a = estate.open()[("ssis-failed", "F/P/Main.dtsx")]
    assert "no job failed with it" in a["message"] and a["next"] == "ssisbliss executions --env prod --id 77"


def test_a_package_failure_a_failed_job_accounts_for_is_one_alert(estate):
    estate.jobs = [job("Nightly", "2026-11-02T01:00:00", status=0)]
    estate.steps = [step("Nightly")]
    estate.history = run_rows("Nightly", 1, "2026-11-02T01:00:00", status=0, steps=[(1, "Load", 0, "Failed. Execution ID: 77")])
    _failed_execution(estate.catalog(), 77)
    estate.check()
    assert list(estate.open()) == [("job-failed", "Nightly")]


def test_a_chain_that_stops_without_a_failure(estate):
    estate.jobs = [job("1 A", "2026-11-02T01:00:00"), job("2 B", "2026-11-01T02:00:00", schedules=0)]
    estate.steps = [step("1 A", 1, "Start next", "TSQL", "EXEC msdb.dbo.sp_start_job @job_name = N'2 B';")]
    estate.check()
    assert "did not start" in estate.open()[("chain-stopped", "1 A")]["message"]


def test_catalog_health_comes_from_ssisbliss(estate):
    cat = estate.catalog()
    cat.health = {"properties": {"OPERATION_CLEANUP_ENABLED": "False", "RETENTION_WINDOW": "365", "SERVER_LOGGING_LEVEL": "1",
                                 "MAX_PROJECT_VERSIONS": "10", "VERSION_CLEANUP_ENABLED": "True"}}
    cat.save()
    estate.check()
    assert ("health", "cleanup") in estate.open()
