"""The period as a timeline: in the report's JSON, in a terminal and on a page."""
from conftest import job, run_rows, step
from test_report import report

from loadwatch import gantt, html_report


def night(estate):
    estate.config('[[expect]]\nenv = "prod"\njob = "Silver"\nby = "04:00"\n[[expect]]\nenv = "prod"\njob = "Gone"\nby = "06:00"\n')
    estate.jobs = [job("Bronze", "2026-11-02T01:00:00", duration=3000), job("Silver", "2026-11-02T01:30:00", duration=10000, status=0),
                   job("Reindex", "2026-11-01T01:00:00", running_since="2026-11-02T07:00:00")]
    estate.steps = [step("Bronze"), step("Silver"), step("Reindex", 1, "Rebuild", "TSQL", "EXEC x")]
    estate.history = run_rows("Bronze", 3, "2026-11-02T01:00:00", duration=3000) + run_rows("Silver", 2, "2026-11-02T01:30:00", status=0, duration=10000)


def test_rows_run_in_start_order_with_what_did_not_run_last(estate):
    night(estate)
    t = report(estate)["timeline"]
    assert [r["name"] for r in t["rows"]] == ["Bronze", "Silver", "Reindex", "Gone"]
    assert t["rows"][-1]["bars"] == [] and t["rows"][-1]["due"] == "2026-11-02T06:00:00"
    assert t["start"] < "2026-11-02T01:00:00" and t["end"] > t["now"]


def test_a_bar_runs_from_start_to_end_and_a_running_job_to_now(estate):
    night(estate)
    rows = {r["name"]: r for r in report(estate)["timeline"]["rows"]}
    assert rows["Bronze"]["bars"] == [{"start": "2026-11-02T01:00:00", "end": "2026-11-02T01:30:00", "status": "ok", "took_s": 1800.0}]
    assert rows["Silver"]["bars"][0]["status"] == "failed" and rows["Silver"]["bars"][0]["end"] == "2026-11-02T02:30:00"
    assert rows["Reindex"]["bars"][0]["status"] == "running" and rows["Reindex"]["bars"][0]["end"] == "2026-11-02T08:00:00"


def test_how_many_ran_at_once():
    at = gantt._at
    bars = [(at("2026-11-02T01:00:00"), at("2026-11-02T02:00:00")), (at("2026-11-02T01:30:00"), at("2026-11-02T03:00:00")),
            (at("2026-11-02T03:00:00"), at("2026-11-02T04:00:00"))]     # the third starts as the second ends: not at once
    steps, peak, when = gantt._concurrency(bars)
    assert (peak, when) == (2, "2026-11-02T01:30:00") and steps[-1][1] == 0


def test_a_deadline_shows_whether_it_was_met(estate):
    night(estate)
    rows = {r["name"]: r for r in report(estate)["timeline"]["rows"]}
    assert rows["Silver"]["due_missed"] is True and rows["Gone"]["due_missed"] is True
    estate.config('[[expect]]\nenv = "prod"\njob = "Bronze"\nby = "04:00"\n')
    rows = {r["name"]: r for r in report(estate)["timeline"]["rows"]}
    assert rows["Bronze"]["due_missed"] is False


def test_a_run_far_longer_than_usual_is_slow_and_needs_a_look(estate):
    estate.jobs = [job("Silver", "2026-11-02T01:00:00", duration=20000)]            # HHMMSS: 2 hours
    estate.history = run_rows("Silver", 9, "2026-11-02T01:00:00", duration=20000)
    estate.history += [r for i in range(4) for r in run_rows("Silver", i + 1, f"2026-10-2{i}T01:00:00", duration=3000)]   # 30 minutes
    doc = report(estate)
    assert doc["activities"][0]["state"] == "slow" and doc["counts"]["slow"] == 1
    assert doc["timeline"]["rows"][0]["bars"][0]["status"] == "slow"
    a = doc["attention"][0]
    assert a["rule"] == "ran-long" and "usually takes 30 min" in a["message"]


def test_the_terminal_timeline_is_plain_ascii(estate):
    night(estate)
    estate.save()
    text, code = estate.cli("report", "--now", "2026-11-02T08:00:00")
    assert code == 0 and "## The period" in text
    drawn = text[text.index("```"):text.rindex("```")]
    assert drawn.isascii() and "X" in drawn and "did not run" in drawn and "D deadline" in drawn


def test_ticks_stay_readable_on_any_span():
    t = gantt._ticks(gantt._at("2026-11-01T00:00:00"), gantt._at("2026-11-04T00:00:00"))
    assert 2 <= len(t) <= 11
    t = gantt._ticks(gantt._at("2026-11-01T08:00:10"), gantt._at("2026-11-01T08:04:00"))
    assert 2 <= len(t) <= 11


def report_activities():
    return [{"source": "prod", "name": "Bronze", "state": "ok", "what": "T-SQL", "usual_s": 600, "runs": [{"start": "2026-11-02T01:00:00", "status": "succeeded", "duration_s": 600}]}]


def page(estate, tmp_path):
    out = tmp_path / "morning.html"
    estate.save()
    doc, code = estate.cli("report", "--format", "html", "--out", str(out), "--now", "2026-11-02T08:00:00")
    assert code == 0 and doc["written"] == str(out.resolve()) and doc["ok"]
    return out.read_text(encoding="utf-8")


def test_the_page_stands_alone_with_the_chart_above_the_table(estate, tmp_path):
    night(estate)
    html = page(estate, tmp_path)
    assert '<svg class="gantt"' in html and html.index("The period") < html.index("What ran (")
    for loads in ("http://", "https://", 'src="', "<link"):
        assert loads not in html.split("<svg", 1)[0].replace('xmlns="http://www.w3.org/2000/svg"', "") and 'src="' not in html
    assert "! did not run" in html and 'class="due missed"' in html and "prefers-color-scheme:dark" in html


def test_a_phone_gets_a_chart_of_its_own_with_names_over_the_bars(estate, tmp_path):
    night(estate)
    html = page(estate, tmp_path)
    assert '<svg class="gantt phone"' in html and "@media (max-width:700px){svg.gantt{display:none}svg.gantt.phone{display:block}}" in html
    drawn = gantt.svg(gantt.timeline(report_activities(), gantt._at("2026-11-02T08:00:00")), phone=True)
    assert 'viewBox="0 0 326 ' in drawn and 'x="2" y="42">Bronze' in drawn


def test_a_page_says_times_as_people_do_and_keeps_commands_exact():
    assert html_report._when("failed at 2026-11-02T04:18:00, and at 2026-11-01T23:05:30", "2026-11-02") == "failed at 04:18, and at 1 Nov 23:05"
    assert html_report._when("Since 2026-11-02 00:00:00", "2026-11-03") == "Since 2 Nov 00:00"
    assert html_report._when("build 2026-11-02 only", "2026-11-02") == "build 2026-11-02 only"


def test_the_first_thing_is_on_the_page_once_and_whole(estate, tmp_path):
    night(estate)
    html = page(estate, tmp_path)
    start, rest = html.split('<section class="card start critical">')[1].split("</section>", 1)
    assert "Start here, on prod" in start and "Why first:" in start and "<code>" in start
    assert "Silver failed at" in start and "Silver failed at" not in rest and " more)</h2>" in rest


def test_server_text_is_escaped_everywhere(estate, tmp_path):
    evil = '<img src=x onerror=alert(1)>"'
    estate.jobs = [job(evil, "2026-11-02T01:00:00", status=0, message=evil)]
    estate.steps = [step(evil, 1, evil, "TSQL", evil)]
    estate.history = run_rows(evil, 1, "2026-11-02T01:00:00", status=0, steps=[(1, evil, 0, evil)])
    html = page(estate, tmp_path)
    assert "<img" not in html and "&lt;img" in html


def test_a_page_says_nothing_ran_without_a_chart(estate, tmp_path):
    assert '<svg class="gantt"' not in page(estate, tmp_path).replace("svg.gantt", "")


def test_runs_close_together_share_one_icon():
    bars = [{"start": f"2026-11-02T01:0{i}:00", "end": f"2026-11-02T01:0{i}:05", "status": "failed", "took_s": 5} for i in range(4)]
    row = {"name": "Often", "state": "failed", "what": "", "usual_s": None, "due": None, "bars": bars}
    t = {"start": "2026-11-02T00:00:00", "end": "2026-11-02T08:00:00", "now": "2026-11-02T08:00:00", "rows": [row],
         "ticks": [], "running": [], "peak": 1, "peak_at": None}
    drawn = gantt.svg(t)
    assert drawn.count('class="bar failed"') == 4 and drawn.count('class="icon"') == 1
