"""Telling someone: an incoming webhook (Slack and Teams both take {"text": ...}), and a heartbeat URL that an outside
service watches, because a monitor that died sends nothing. Without a webhook, alerts stay in `check`'s output."""
from __future__ import annotations

import json
import urllib.request

ICON = {"critical": "CRITICAL", "warning": "warning", "info": "info"}


def text(opened: list[dict], closed: list[dict]) -> str:
    lines = []
    for a in opened:
        lines.append(f"{ICON[a['severity']]} [{a['env']}] {a['message']}" + (f"\n    {a['next']}" if a["next"] else ""))
    lines += [f"resolved [{a['env']}] {a['rule']}: {a['subject']}" for a in closed]
    return "\n".join(lines)


def _post(url: str, body: dict | None = None) -> None:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(request, timeout=15):
        pass


def send(webhook: str | None, opened: list[dict], closed: list[dict]) -> str | None:
    """Why it could not be sent, or None. A failure leaves the alerts untold, so the next pass tries again."""
    if not webhook or not (opened or closed):
        return None
    try:
        _post(webhook, {"text": text(opened, closed)})
    except OSError as e:
        return f"webhook: {e}"
    return None


def beat(url: str | None) -> str | None:
    if not url:
        return None
    try:
        _post(url)
    except OSError as e:
        return f"heartbeat: {e}"
    return None
