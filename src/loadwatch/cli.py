"""`loadwatch report` in the morning: what ran today and what needs your attention. `loadwatch check` from a scheduler every
few minutes, to be told as it happens. The same output contract as ssisbliss: stdout is one JSON document, with `ok`,
`error_kind` and `next`, except `report`'s default, which is for reading."""
from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

from ssisbliss.ssis.contract import EXIT_USAGE, Parser, UsageError, envelope, usage_doc

from . import config, discover, html_report, notify, report
from .activity import Result
from .sources import build
from .sources.fabric import FabricClient
from .sources.fabric import client as fabric_client
from .store import Store

COMMANDS = ["discover", "report", "check", "status", "alerts", "ack"]
SEVERITIES = ("critical", "warning", "info")


def _now(a) -> datetime:
    at = getattr(a, "now", None) or os.environ.get("LOADWATCH_NOW")
    return datetime.fromisoformat(at) if at else datetime.now().replace(microsecond=0)


def _public(alert: dict) -> dict:
    return {k: v for k, v in alert.items() if v is not None and k not in ("told_at", "told_resolved_at")}


def _pass(cfg: config.Config, store: Store, estate: discover.Estate, client: FabricClient, since: datetime, now: datetime,
          thorough: bool) -> list[Result]:
    """Every source once: what it found goes into the alerts, and what it wants to remember into the store."""
    results = []
    for source in build(cfg, estate, client):
        r = source.collect(since, now, store.memory(source.name), thorough)
        store.apply(r.source, r.signals, r.looked, now)
        store.remember(r.source, r.memory, now, r.location, r.unread)
        results.append(r)
    return results


def cmd_report(a, cfg: config.Config, store: Store) -> tuple[dict | str, int]:
    """Nothing has to have run before: a report reads every job's history itself."""
    now = _now(a)
    since = datetime.fromisoformat(a.since) if a.since else now.replace(hour=0, minute=0, second=0, microsecond=0)
    client = fabric_client()
    estate = discover.current(cfg, now, a.rediscover, client)
    results = _pass(cfg, store, estate, client, since, now, thorough=True)
    doc = report.build(results, store.alerts(everything=True), since, now, estate)
    if a.format == "json":
        return doc, 0
    text = report.markdown(doc) if a.format == "markdown" else html_report.page(doc)
    if not a.out:
        return text, 0
    out = Path(a.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")     # a shell redirect may not: PowerShell's `>` writes UTF-16
    if a.open:
        webbrowser.open(out.as_uri())
    return {"verdict": doc["verdict"], "written": str(out)}, 0


def cmd_check(a, cfg: config.Config, store: Store) -> tuple[dict, int]:
    now = _now(a)
    since = now - timedelta(hours=cfg.window_hours)
    client = fabric_client()
    estate = discover.current(cfg, now, client=client)
    environments = [{"env": r.source, "server": r.location, "jobs": r.watched, "signals": len(r.signals),
                     **({"unread": r.unread} if r.unread else {})}
                    for r in _pass(cfg, store, estate, client, since, now, thorough=False)]
    opened, closed = store.to_tell(now, cfg.remind_hours)
    problems = [p for p in (notify.send(cfg.webhook, opened, closed), notify.beat(cfg.heartbeat)) if p]
    if not any(p.startswith("webhook") for p in problems):
        store.told(opened, closed, now)
    alerts = store.alerts()
    return {"at": now.isoformat()[:19], "environments": environments, "summary": _summary(alerts),
            "new": [_public(x) for x in opened], "resolved": [_public(x) for x in closed],
            **({"problems": problems} if problems else {})}, 0


def cmd_discover(a, cfg: config.Config, store: Store) -> tuple[dict | str, int]:
    estate = discover.discover(cfg, _now(a), a.network, a.server)
    doc = report.estate_doc(estate)
    return (doc if a.format == "json" else report.estate_text(doc)), 0


def _summary(alerts: list[dict]) -> str:
    counts = [f"{sum(x['severity'] == s for x in alerts)} {s}" for s in SEVERITIES if any(x["severity"] == s for x in alerts)]
    return ", ".join(counts) + " open" if counts else "nothing open"


def cmd_status(a, cfg: config.Config, store: Store) -> tuple[dict, int]:
    alerts = store.alerts()
    return {"summary": _summary(alerts), "environments": store.passes(), "open": [_public(x) for x in alerts]}, 0


def cmd_alerts(a, cfg: config.Config, store: Store) -> tuple[dict, int]:
    return {"alerts": [_public(x) for x in store.alerts(a.all)]}, 0


def cmd_ack(a, cfg: config.Config, store: Store) -> tuple[dict, int]:
    if not store.ack(a.id, a.note, _now(a)):
        return {"error": f"no open alert {a.id}", "next": ["loadwatch alerts"]}, 2
    return {"acknowledged": a.id}, 0


def parser() -> Parser:
    p = Parser(prog="loadwatch", description=__doc__)
    p.add_argument("--config", help=f"default: ./{config.FILE}, then ~/.config/loadwatch/{config.FILE}")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    s = sub.add_parser("discover", help="work out what can be watched: SQL Servers and Fabric workspaces")
    s.add_argument("--network", action="store_true", help="also ask the domain and the local network, and try a Windows login "
                                                          "on every SQL Server they name")
    s.add_argument("--server", action="append", default=[], help="a SQL Server to add (repeat)")
    s.add_argument("--format", choices=["text", "json"], default="text")
    s.add_argument("--now", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_discover)
    s = sub.add_parser("report", help="the morning report: everything that ran today, and what needs your attention")
    s.add_argument("--since", help="start of the period (default: midnight today), e.g. 2026-11-01T18:00:00")
    s.add_argument("--format", choices=["markdown", "html", "json"], default="markdown",
                   help="markdown for a terminal; html for a page with the timeline chart; json for agents")
    s.add_argument("--rediscover", action="store_true", help="look for what to watch again first (it happens daily anyway)")
    s.add_argument("--out", help="write to this file instead of stdout")
    s.add_argument("--open", action="store_true", help="with --out: open it in the browser")
    s.add_argument("--now", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("check", help="one pass for a scheduler: update the alerts, tell someone")
    s.add_argument("--now", help="the clock, for tests and replays")
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("status", help="what is open, and when each environment was last looked at")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("alerts", help="open alerts")
    s.add_argument("--all", action="store_true", help="resolved ones too")
    s.set_defaults(fn=cmd_alerts)
    s = sub.add_parser("ack", help="acknowledge an alert: it stays open, and nobody is reminded of it")
    s.add_argument("id", type=int)
    s.add_argument("--note")
    s.add_argument("--now", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_ack)
    return p


def run(argv: list[str]) -> tuple[dict | str, int]:
    a = None
    try:
        a = parser().parse_args(argv)
        cfg = config.load(a.config)
        doc, code = a.fn(a, cfg, Store(cfg.db))
    except UsageError as e:
        doc, code = usage_doc(e, argv, COMMANDS), EXIT_USAGE
    except ValueError as e:
        doc, code = {"error": str(e)}, 2
    except Exception as e:  # noqa: BLE001 - always answer in JSON
        doc, code = {"error": f"{type(e).__name__}: {e}"}, 1
    return (doc if isinstance(doc, str) else envelope(doc, code)), code


def main(argv: list[str] | None = None) -> int:
    doc, code = run(sys.argv[1:] if argv is None else list(argv))
    if isinstance(doc, str):
        if hasattr(sys.stdout, "reconfigure"):      # job names and messages are not limited to a Windows console's code page
            sys.stdout.reconfigure(errors="replace")
        sys.stdout.write(doc)
    else:
        json.dump(doc, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
