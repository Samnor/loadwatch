# loadwatch

**Every morning: what ran, and what needs your attention.** Across your SQL Servers, where SQL Agent jobs run SSIS packages,
PowerShell and Python, and your Fabric workspaces, with their pipelines, notebooks and semantic model refreshes.

```bash
pip install git+https://github.com/Samnor/loadwatch
loadwatch report
```

That is the whole setup. It works out what it can watch: the SQL Servers this machine and your SQL tools know about, logging
in with Windows integrated auth, and the Fabric workspaces your `az login` can see. Built on
[ssisbliss](https://github.com/Samnor/ssisbliss), which pip brings along.

```bash
loadwatch discover                                     # what it found, what it can do with each, and why not
loadwatch report --format html --out morning.html --open    # the page, with the timeline chart
loadwatch report --since 2026-11-01T18:00:00           # a longer night
loadwatch report --format json                         # for agents and scripts
```

The report opens with where to start and why, and says what it looked at. It needs nothing running before it: it reads every job's history itself. It also keeps a little memory, so each
morning's report knows more than the last.

```
# 2 things need your attention

Since 2026-11-02 00:00:00, at 2026-11-02 08:10:00: 3 ran: 1 fine, 1 failed, 1 suspect, 0 recovered, 0 still running.

## Needs you now (1)

- **[prod] 2 Silver load**: 2 Silver load failed at 2026-11-02T01:15:00: step 1 (Load). Execution ID: 77 Package task Load:
  Failed to update database GOLD_ARCHIVE because the database is read-only.
  - `ssisbliss executions --env prod --id 77`

## Needs a look (1)

- **[prod] Archive files / Archive**: Archive files reports success, but step 1 (Archive) failed and the job went on:
  Access denied to \\files\archive
```

## The timeline

The HTML report draws the period as a Gantt chart: one row per job, time across, a bar for every run, in the order the
night ran. [docs/sample-report.html](docs/sample-report.html) is a sample from a fake estate. The page is one self-contained
file, so it opens from disk and goes in an email, in light or dark, and on a phone the chart redraws with names over the bars. A terminal gets the same chart in plain ASCII, and
`--format json` has it as data under `timeline`.

Beyond start and end, it shows what a morning needs:

- **What did not run:** a job with a missed deadline, or overdue, is a labelled row with no bar. The silence is the finding.
- **Each run against its usual duration:** a tick on the bar, so an overrun needs no reading.
- **State:** succeeded, slow, suspect, failed, canceled and running, each with its own colour and, where it matters, an icon.
- **Deadlines:** a diamond on the job's row, filled when missed.
- **How many ran at once:** a strip under the chart with the peak named, for contention.
- **Now,** so a job still running reads as running.

Jobs that ran nowhere near the period, and were not expected to, are left out. Hover or focus a bar for the times, the usual
duration and what it runs. Every run is also in the table below the chart.

## What it needs

- **Python 3.11 or later.** `pip install git+https://...` needs git; without it, use
  `pip install https://github.com/Samnor/loadwatch/archive/main.zip`.
- **For SQL Server:** [ODBC Driver 17 or 18](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server),
  and a Windows login that can read. Reading `msdb` takes `SQLAgentReaderRole`, and SSISDB takes `ssis_logreader`. Whatever it
  cannot read shows up as a blind spot, not as good news. This has not been tried yet with a login that has only those roles.
- **For Fabric:** the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), and `az login`. No secret is stored:
  tokens are asked for from az each time. A semantic model's refresh history takes Write permission on it.

Either alone is fine: what it cannot look at, it says so, with what to do about it.

## How it finds things

| Where | When |
|---|---|
| The SQL Server instances on this machine | always |
| Servers registered or recently used in SSMS, Azure Data Studio and VS Code | always. Names only: nothing stored with them is read |
| `ssisbliss.toml`, and `$LOADWATCH_SERVERS` (comma separated) | always |
| `--server NAME` | when you add one, and it is remembered |
| Active Directory service names, and SQL Server Browser on the local network | only with `loadwatch discover --network` |
| Fabric workspaces your `az login` can see | always |

`--network` is opt-in on purpose. It tries a Windows login on every SQL Server it finds, which to a security team looks like
someone probing the estate, so check before you run it on a corporate network.

Each server is then tried: it reads the jobs through ssisbliss, verifying the certificate first, and if the certificate cannot
be verified it trusts it for reading and says so. What it found is kept in `~/.loadwatch/estate.json` and refreshed daily. A
server that could be watched and stops answering stays in the list and becomes an alert: it never quietly disappears.

## Optional: what only you know

No server knows that the sales marts must be done by 06:00. A `loadwatch.toml`, in the working folder or in
`~/.config/loadwatch/`, adds that: see [loadwatch.example.toml](loadwatch.example.toml). `env` is the server's name as `discover`
shows it. If the file lists `environments`, those are watched instead of discovering anything.

## What it notices

| Rule | | Needs |
|---|---|---|
| `nothing-started` | Most of a server's scheduled jobs have stopped starting: look at SQL Server Agent | |
| `shared-cause` | The same error in several jobs: one finding, and one fix | |
| `job-failed` | A job failed, with the step, and for a package the task, error, database and usual cause. It says if it is new or how long it has failed, what it blocked, and who deployed just before | |
| `deadline-missed` | A job has not succeeded by the time you said | `[[expect]]` |
| `overdue` | Longer without a run than the job's longest recent gap | |
| `missed-schedule` | Due, and did not start | memory |
| `job-unscheduled` | Someone disabled it or removed its schedule | memory |
| `overrunning` | Running far longer than usual | |
| `step-failed-silently` | The job reports success, but a step failed and it went on | |
| `script-output-error` | A PowerShell or Python step exited 0 with a traceback in its output | |
| `ssis-failed` | A package failed and no job failed with it: run by hand, or started without waiting for it | |
| `chain-stopped`, `chain-missing-job` | A job chain stopped with nothing failed, or starts a job that does not exist | |
| `ran-long` | A job's run took far longer than usual. It worked, this time, and whatever came after it started late | |
| `flaky` | Fine tonight, and failing on and off | |
| `creeping` | Slower night after night, with no one bad night | |
| `slower` | A step took much longer than usual | |
| `health` | The SSIS catalog's own health: cleanup, retention, log growth | |
| `fabric-failed` | A pipeline, notebook or other Fabric item failed, or a semantic model refresh, with the service's reason. `overrunning`, `ran-long`, `overdue`, `flaky` and `creeping` apply to them too | |
| `server-unreachable`, `blind-spot` | Something could not be read. No answer is never good news: it clears no alert | |

`loadwatch ack ID --note "..."` moves an alert out of the attention list and stops reminders. It stays open until it is over.

## Being told as it happens

`loadwatch check` is the same thing for a scheduler, every few minutes: it reads only what changed (0.2 s on the lab), keeps
alerts from open to resolved, and sends new ones to the webhook in `$LOADWATCH_WEBHOOK` (Slack or Teams). `heartbeat` in
loadwatch.toml pings a URL after each pass, so that a dead monitor is noticed by someone else. It should run away from the
servers it watches.

## How it fits together

```
ssisbliss commands  ->  observe  ->  rules  ->  store  ->  report, notify
(msdb, SSISDB)          one pass     pure       alerts,
                                                memory
```

`ssisbliss.ssis.cli.run` is the API for SQL Server: its JSON documents are stable, and every command used is read-only.
loadwatch has no SQL of its own. Fabric is read over its REST APIs. What the report shows comes from `activity.py`'s shapes, so
a new source adds a module and nothing else:

```python
class Source(Protocol):
    def collect(self, since, now, memory, thorough) -> Result: ...      # activities and signals
```

## What ssisbliss should expose

Things loadwatch had to work around. Each belongs there, not here.

- **The server's clock.** No document says what time it is on the server, so loadwatch assumes the watcher and the server
  share a time zone. Deadlines and `overdue` depend on it.
- **Job schedules.** `overdue` guesses from gaps between runs, and `sysschedules` would say exactly when a job was due.
- **All jobs' runs at once.** A report reads history one job at a time, a query per job (1 s for 20 jobs, so a few hundred
  should still be fine). `history --since` over all jobs would be one query.
- **More of a step's output.** A succeeded step's message is cut to 200 characters, so `script-output-error` can miss a
  traceback further in. SQL Agent also keeps only the first part of the output; a job's own output file would need reading.
- **`_with_target`** is private, and loadwatch uses it to make `next` commands ready to run. It also quotes for a POSIX shell,
  which cmd.exe does not read, so loadwatch swaps the quotes.
- **A stable API.** loadwatch pins one ssisbliss commit, because `cli.run`'s documents and `_with_target` are not promised yet.

## Status

Early, and checked on real systems. A clean `pip install` on Windows Server with SQL Server 2019, in an empty folder with no
config, found both instances on the machine and their SSIS catalogs in 1 s and reported in about 2 s, logging in with Windows
integrated auth as a sysadmin. On real failures it caught a PowerShell `throw`, a failed step in a job that went on, a traceback
with exit code 0, a job being disabled, and a broken chain that then recovered. On a real Fabric tenant it found 4 workspaces and
26 items with no setup, and read their pipelines' and refreshes' history in about 3 s.

Only run against fakes so far: `missed-schedule`, `overdue`, `deadline-missed`, the webhook, the SSMS and other tool finders,
`--network`, a failed Fabric pipeline or refresh (the tenant had none), and running on a Windows machine that also has `az`. It
has not met a production estate, so expect its thresholds to need tuning.

Not built: deadlines for Fabric items, alerts for a job that disappears, confirming a failure over two passes before alerting,
sending the report by email, each step as a segment of its bar, and history kept longer than msdb keeps it.

## Development

```bash
uv sync
uv run pytest
```

`uv sync` uses the ssisbliss checkout next to this folder; pip uses the commit pinned in pyproject.toml. See [AGENTS.md](AGENTS.md).
