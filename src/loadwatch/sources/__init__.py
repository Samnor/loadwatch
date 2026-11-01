"""Where loadwatch looks. Every source returns the same shapes (activity.py); adding one takes a module here and a line
in `build`."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..activity import Source
from ..config import Config
from .fabric import FabricClient, FabricSource
from .sqlserver import SqlServerSource

if TYPE_CHECKING:
    from ..discover import Estate


def build(cfg: Config, estate: "Estate", client: FabricClient | None = None) -> list[Source]:
    out: list[Source] = []
    fabric = client or FabricClient()    # one client: tokens are asked for once
    for t in estate.targets:
        if not t.watched:
            continue
        if t.kind == "sql_server":
            out.append(SqlServerSource(t.env or t.name, t.args(cfg.ssisbliss_config), cfg))
        elif t.kind == "fabric_workspace":
            out.append(FabricSource(t.name, t.id or "", cfg, fabric))
    return out
