"""Working out what to watch, with nothing configured."""
import json
from datetime import datetime
from pathlib import Path

import pytest
from conftest import job
from ssisbliss.ssis.fake import FakeCatalog

from loadwatch import cli, config, discover
from loadwatch.sources import fabric

NOW = datetime(2026, 11, 2, 8, 0, 0)


def no_az(resource, timeout=60):
    raise ValueError("the Azure CLI is not on PATH: install it and run `az login`")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A machine with no config, no tools and no az."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOADWATCH_SERVERS", raising=False)
    monkeypatch.delenv("SSISBLISS_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fabric, "az_token", no_az)
    return tmp_path


def server(root: Path, name="prod", jobs=()) -> str:
    d = root / name
    d.mkdir()
    FakeCatalog("LAB\\PROD", major=15, user="LAB\\alice").save(d)
    (d / "msdb.json").write_text(json.dumps({"server_name": "LAB\\PROD", "jobs": [*jobs, job("SSIS Server Maintenance Job", "2026-11-01T00:00:00")],
                                             "steps": [], "history": []}))
    return f"fake://{d}"


def test_nothing_found_says_what_to_do(home):
    doc, code = cli.run(["discover", "--format", "json"])
    assert code == 0 and doc["watched"] == []
    notes = " ".join(doc["notes"])
    assert "az login" in notes and "--network" in notes and "--server NAME" in notes


def test_the_report_runs_with_no_setup_and_says_there_is_nothing_yet(home):
    doc, code = cli.run(["report", "--format", "json"])
    assert code == 0 and doc["verdict"] == "Nothing to watch yet" and doc["notes"]


def test_a_server_is_found_tried_and_watched_with_no_config(home, monkeypatch):
    monkeypatch.setenv("LOADWATCH_SERVERS", server(home, jobs=[job("Nightly", "2026-11-02T01:00:00", status=0)]))
    doc, code = cli.run(["discover", "--format", "json"])
    [t] = doc["watched"]
    assert code == 0 and t["can"] == ["SQL Agent jobs", "SSIS catalog"] and t["size"] == 2 and t["found_by"] == ["LOADWATCH_SERVERS"]
    doc, code = cli.run(["report", "--format", "json", "--now", "2026-11-02T08:00:00"])
    assert code == 0 and doc["attention"][0]["rule"] == "job-failed"
    assert doc["attention"][0]["next"].startswith("ssisbliss history --server fake://")


def test_a_report_discovers_by_itself_on_first_run(home, monkeypatch):
    monkeypatch.setenv("LOADWATCH_SERVERS", server(home))
    doc, _ = cli.run(["report", "--format", "json", "--now", "2026-11-02T08:00:00"])
    assert len(doc["sources"]) == 1 and (home / ".loadwatch" / "estate.json").is_file()


def test_a_server_that_cannot_be_read_is_listed_with_why_not_alarmed(home, monkeypatch):
    monkeypatch.setenv("LOADWATCH_SERVERS", f"{server(home)},fake://{home}/nothing-here")
    doc, _ = cli.run(["report", "--format", "json", "--now", "2026-11-02T08:00:00"])
    assert len(doc["sources"]) == 1 and [t["name"].endswith("nothing-here") for t in doc["not_watched"]] == [True]
    assert doc["attention"] == []
    text, _ = cli.run(["report", "--now", "2026-11-02T08:00:00"])
    assert "## Not watched" in text


def test_a_watched_server_that_stops_answering_stays_watched_and_alerts(home, monkeypatch):
    fake = server(home)
    monkeypatch.setenv("LOADWATCH_SERVERS", fake)
    cli.run(["discover"])
    (home / "prod" / "msdb.json").unlink()
    doc, _ = cli.run(["report", "--rediscover", "--format", "json", "--now", "2026-11-02T08:00:00"])
    assert [a["rule"] for a in doc["attention"]] == ["server-unreachable"] and doc["verdict"].startswith("1 thing")


def test_the_network_is_only_asked_when_you_ask(home, monkeypatch):
    def called():
        raise AssertionError("asked the network without --network")
    monkeypatch.setattr(discover, "from_domain", called)
    monkeypatch.setattr(discover, "from_browser", called)
    cli.run(["discover"])
    cli.run(["report", "--format", "json"])
    monkeypatch.setattr(discover, "from_domain", lambda: [])
    monkeypatch.setattr(discover, "from_browser", lambda: [f"fake://{home}/none"])
    doc, _ = cli.run(["discover", "--network", "--format", "json"])
    assert doc["not_watched"][0]["found_by"] == ["the network"]


def test_a_daily_rediscovery_keeps_what_you_added(home):
    fake = server(home)
    cli.run(["discover", "--server", fake])
    cfg = config.load()
    estate = discover.load(cfg)
    estate.at = "2026-10-01T00:00:00"
    discover.save(cfg, estate)
    again = discover.current(cfg, NOW, client=fabric.FabricClient())
    assert [t.name for t in again.targets] == [fake] and again.at == NOW.isoformat()[:19]


def test_a_recent_estate_is_not_looked_for_again(home, monkeypatch):
    cli.run(["discover", "--now", "2026-11-02T07:00:00"])
    monkeypatch.setattr(discover, "discover", lambda *a, **k: pytest.fail("looked again"))
    discover.current(config.load(), NOW)


def test_loadwatch_toml_names_what_to_watch_and_nothing_is_discovered(home, monkeypatch):
    (home / "loadwatch.toml").write_text('environments = ["prod"]\n')
    monkeypatch.setattr(discover, "discover", lambda *a, **k: pytest.fail("discovered"))
    estate = discover.current(config.load(), NOW)
    assert [(t.env, t.can) for t in estate.targets] == [("prod", ["SQL Agent jobs"])]


def test_a_certificate_it_cannot_verify_is_tried_once_and_said_out_loud(home, monkeypatch):
    seen = []

    def ask(target, *command):
        seen.append(target)
        if "--trust-server-certificate" not in target:
            return {"ok": False, "error": "SSL Provider: The certificate chain was issued by an authority that is not trusted"}
        return {"ok": True, "jobs": [{}], "has_ssisdb": True}
    monkeypatch.setattr(discover, "ask", ask)
    monkeypatch.setenv("LOADWATCH_SERVERS", "SQL01")
    doc, _ = cli.run(["discover", "--format", "json"])
    assert doc["watched"][0]["trust"] is True and doc["watched"][0]["can"] == ["SQL Agent jobs", "SSIS catalog"]
    assert any("could not be verified" in n for n in doc["notes"])
    assert seen[0] == ["--server", "SQL01"]      # the verified way is tried first


@pytest.mark.parametrize("error, says", [("Login failed for user 'X'", "login was refused"), ("Login timeout expired", "did not answer"),
                                         ("No SQL Server ODBC driver found (install ODBC Driver 18)", "ODBC Driver 18")])
def test_why_a_server_cannot_be_watched_is_plain(error, says):
    assert says in discover._why(error)


def test_servers_are_read_from_your_tools_and_nothing_else_from_them(tmp_path):
    ssms = tmp_path / "Microsoft/SQL Server Management Studio/20.0"
    ssms.mkdir(parents=True)
    (ssms / "RegSrvr.xml").write_text('<x><sfc:Value>server=SQL01\\PROD;integrated security=true;password=hunter2</sfc:Value>'
                                      '<y>Data Source=SQL02;pooling=false</y></x>')
    (ssms / "UserSettings.xml").write_text("<a><Instance>SQL03</Instance><Instance>SQL01\\PROD</Instance></a>")
    for app in ("azuredatastudio", "Code"):
        (tmp_path / app / "User").mkdir(parents=True)
    (tmp_path / "azuredatastudio/User/settings.json").write_text('{ // comment\n "datasource.connections": [{"options": {"server": "SQL04"}}]}')
    (tmp_path / "Code/User/settings.json").write_text('{"mssql.connections": [{"server": "SQL05\\\\DEV", "password": "hunter2"}]}')
    found = discover.from_tools(tmp_path)
    assert set(found) == {"SQL01\\PROD", "SQL02", "SQL03", "SQL04", "SQL05\\DEV"} and "hunter2" not in str(found)


def test_what_cannot_run_agent_jobs_is_left_out(home, monkeypatch):
    monkeypatch.setattr(discover, "from_tools", lambda: ["(localdb)\\MSSQLLocalDB", "x.database.windows.net", "abc.datawarehouse.fabric.microsoft.com", "SQL01"])
    assert [t.name for t in discover.find_sql(config.load(), False, [])] == ["SQL01"]


def test_the_same_server_from_two_places_is_one_target(home, monkeypatch):
    monkeypatch.setenv("LOADWATCH_SERVERS", "SQL01")
    monkeypatch.setattr(discover, "from_tools", lambda: ["sql01"])
    [t] = discover.find_sql(config.load(), False, [])
    assert t.found_by == ["LOADWATCH_SERVERS", "your SQL tools"]


def test_an_environment_in_ssisbliss_toml_is_found_with_its_login(home, monkeypatch):
    fake = server(home)
    (home / "ssisbliss.toml").write_text(f'[environments.prod]\nserver = "{fake}"\n')
    doc, _ = cli.run(["discover", "--format", "json"])
    assert doc["watched"][0]["env"] == "prod"


def test_browser_and_domain_answers_are_read():
    reply = b"\x05\x00\x00ServerName;SQL01;InstanceName;PROD;IsClustered;No;Version;15.0;tcp;14331;;ServerName;SQL02;InstanceName;MSSQLSERVER;IsClustered;No;Version;16.0;tcp;1433;;"
    assert discover.parse_browser(reply) == ["SQL01\\PROD", "SQL02"]
    spns = "CN=x\n\tMSSQLSvc/sql01.corp.example:1433\n\tMSSQLSvc/sql02.corp.example:PROD\n\tMSSQLSvc/sql03.corp.example:14331\n\tMSSQLSvc/sql04.corp.example\n"
    assert discover.parse_spns(spns) == ["sql01.corp.example", "sql02.corp.example\\PROD", "sql03.corp.example,14331", "sql04.corp.example"]


def test_next_commands_can_be_pasted_into_cmd_exe(home, monkeypatch):
    fake = server(home, jobs=[job("Nightly", "2026-11-02T01:00:00", status=0)])
    named = f"{fake}"
    monkeypatch.setenv("LOADWATCH_SERVERS", named)
    doc, _ = cli.run(["report", "--format", "json", "--now", "2026-11-02T08:00:00"])
    assert "'" not in doc["attention"][0]["next"]
    from argparse import Namespace

    from loadwatch.rules import _with_target
    quoted = _with_target('ssisbliss jobs --brief', Namespace(env=None, server="HOST\\SQLPROD"))
    assert "'" in quoted         # what the rule above has to undo, until ssisbliss quotes for Windows


def test_a_report_says_why_fabric_was_not_looked_at(home, monkeypatch):
    monkeypatch.setenv("LOADWATCH_SERVERS", server(home))
    doc, _ = cli.run(["report", "--format", "json", "--now", "2026-11-02T08:00:00"])
    assert "not on PATH" in doc["covered"]["fabric"] and len(doc["covered"]["sql_servers"]) == 1
