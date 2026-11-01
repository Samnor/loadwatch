# Working on loadwatch

Watches SQL Server data platforms (SQL Agent jobs running SSIS packages, PowerShell and Python) and Fabric workspaces, with no
setup: `pip install` and run. Run tests with `.venv/bin/python -m pytest -q`, a few seconds. Run the tool with `.venv/bin/loadwatch`.

**ssisbliss is the building block, in `../Onpremtools`.** Reading msdb and SSISDB, job and chain views, step
parsing, health, failure reports, known errors, environments and logins, the JSON contract and fake servers all
live there. Do not write a query or a parser here. When something is missing, it belongs in ssisbliss: add it
there, or list it under "What ssisbliss should expose" in README.md. New here is only what needs time: comparing
passes, deadlines, alert state, telling someone.

## Where things are

In `src/loadwatch/`. Imports go down this list.

| Module | What it does |
|---|---|
| `cli.py` | `check`, `status`, `alerts`, `ack`. Keeps ssisbliss's output contract |
| `store.py` | SQLite: alerts from open to resolved, each job's memory, the last pass per environment |
| `report.py` | The report as data and as markdown. `html_report.py` is the page |
| `gantt.py` | The timeline: `timeline()` is data, `text()` and `svg()` draw it. Escape all server text |
| `notify.py` | Webhook and heartbeat |
| `discover.py` | Works out what to watch: finders, trying each one, the estate in estate.json |
| `sources/` | `build()` makes one `Source` per target. `sqlserver.py` wraps the two below. `fabric.py` is the client, and `judge()` is pure |
| `rules.py` | SQL Server: `evaluate(observation, config, memory, now)`: pure functions, no server, clock or database |
| `observe.py` | One pass on one server, through `ssisbliss.ssis.cli.run`: its JSON documents are the API |
| `activity.py` | The shapes every source returns: `Signal`, `Run`, `Activity`, `Result`, `Source` |
| `config.py` | The optional `loadwatch.toml`: deadlines, thresholds, or naming what to watch |

## Recipes

- **A rule**: a `Signal` in `rules.py`, its name and the part it reads in `RULES`, a test in `tests/test_rules.py`.
- **Something new to read**: an `ask(...)` of an ssisbliss command in `observe.py`, as a part of `Observation`.
- **A new source**: a module in `sources/` with `collect()`, a finder in `discover.py`, a line in `sources.build`.

## Rules that hold everywhere

- Never change a monitored server: only read-only ssisbliss commands.
- No answer is not good news: a rule whose part could not be read must not resolve an alert, and what could
  not be read is itself reported (`blind-spot`, `server-unreachable`).
- An alert says what to run next.
- Discovery tries a login only on what this machine and user already know. Anything wider stays behind `--network`.
- A token goes only to the Fabric and Power BI API hosts, and is never stored or printed.
- No private names: mock estates use textbook names. Commit as `Samnor <3505869+Samnor@users.noreply.github.com>`.
