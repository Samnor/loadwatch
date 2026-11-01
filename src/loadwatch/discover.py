"""Working out what there is to watch, with nothing configured.

SQL Servers: what this machine and this user already know: the SQL Server instances on it, the servers registered or
recently used in SSMS, Azure Data Studio and VS Code, ssisbliss.toml, and $LOADWATCH_SERVERS. `--network` also asks the
domain (MSSQLSvc service names) and the local network (SQL Server Browser). That is opt-in on purpose: it means a Windows
login attempt on every SQL Server found, which a security team would rightly want to know about.

Fabric: the workspaces the `az login` account can see.

Each one found is then tried, through ssisbliss for SQL Server, and the estate records what it can really do. It is kept in
estate.json next to the alerts, and refreshed once a day. A target that could be watched and stops answering stays in it,
because no answer is not good news: its next pass reports it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .observe import ask
from .sources.fabric import FabricClient, watchable
from .sources.fabric import client as fabric_client

MAX_AGE = timedelta(hours=24)
_NOT_SQL_AGENT = re.compile(r"\(localdb\)|\.database\.windows\.net|\.fabric\.microsoft\.com|\.database\.fabric", re.I)
_SERVER_IN_TEXT = re.compile(r"(?:data source|server)\s*=\s*([^;<\"&]+)", re.I)
_CERTIFICATE = re.compile(r"certificate", re.I)
PROBES = 8


@dataclass
class Target:
    kind: str                     # sql_server, fabric_workspace
    name: str
    found_by: list[str] = field(default_factory=list)
    can: list[str] = field(default_factory=list)     # what it can be watched for; empty: it cannot be
    why_not: str | None = None
    env: str | None = None        # an environment in ssisbliss.toml
    trust: bool = False           # its certificate could not be verified, and is trusted to read from it
    id: str | None = None         # a Fabric workspace
    size: int = 0                 # jobs, or items

    @property
    def watched(self) -> bool:
        return bool(self.can)

    def args(self, config: str | None = None) -> list[str]:
        """How ssisbliss reaches it."""
        if self.env:
            return ["--env", self.env, *(["--config", config] if config else [])]
        return ["--server", self.name, *(["--trust-server-certificate"] if self.trust else [])]


@dataclass
class Estate:
    at: str
    targets: list[Target] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)      # what could not be looked at, and how to fix it


# ------------------------------------------------------------------------------------- SQL Server: where to look --

def from_environment() -> list[str]:
    return [s.strip() for s in os.environ.get("LOADWATCH_SERVERS", "").split(",") if s.strip()]


def from_ssisbliss(config: str | None = None) -> dict[str, str]:
    """ssisbliss.toml: environment -> server."""
    import tomllib

    from ssisbliss.ssis.config import find_config
    path = Path(config) if config else find_config()
    if path is None or not path.is_file():
        return {}
    try:
        return {name: e["server"] for name, e in tomllib.loads(path.read_text()).get("environments", {}).items() if e.get("server")}
    except (OSError, ValueError, KeyError):
        return {}


def local_instances() -> list[str]:
    """The SQL Server instances installed on this machine, from the registry."""
    if sys.platform != "win32":
        return []
    import winreg
    host = socket.gethostname()
    out = []
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL",
                                0, winreg.KEY_READ | view) as key:
                for i in range(winreg.QueryInfoKey(key)[1]):
                    name = winreg.EnumValue(key, i)[0]
                    out.append(host if name.upper() == "MSSQLSERVER" else f"{host}\\{name}")
        except OSError:
            continue
    return list(dict.fromkeys(out))


def _app_data() -> Path | None:
    roaming = os.environ.get("APPDATA")
    return Path(roaming) if roaming else None


def from_tools(roaming: Path | None = None) -> list[str]:
    """Servers this user has registered or connected to in SSMS, Azure Data Studio and VS Code. Names only: nothing stored
    with them is read."""
    roaming = roaming or _app_data()
    if roaming is None:
        return []
    out: list[str] = []

    def text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return ""
    for path in [*roaming.glob("Microsoft/SQL Server Management Studio/*/RegSrvr.xml"),
                 *roaming.glob("Microsoft/Microsoft SQL Server/*/Tools/Shell/RegSrvr.xml")]:
        out += _SERVER_IN_TEXT.findall(text(path))
    for path in roaming.glob("Microsoft/SQL Server Management Studio/*/UserSettings.xml"):
        out += re.findall(r"<Instance>([^<]+)</Instance>", text(path))
    for path in (roaming / "azuredatastudio/User/settings.json", roaming / "Code/User/settings.json"):
        out += re.findall(r'"server"\s*:\s*"([^"]+)"', text(path))
    return [s.replace("\\\\", "\\").strip() for s in out if s.strip()]


def parse_browser(reply: bytes) -> list[str]:
    """A SQL Server Browser answer: ServerName;H;InstanceName;I;IsClustered;No;Version;15.0;tcp;1433;;ServerName;..."""
    out = []
    for block in reply[3:].decode("latin-1", errors="replace").split(";;"):
        parts = block.strip(";").split(";")
        fields = dict(zip(parts[::2], parts[1::2]))
        if fields.get("ServerName"):
            instance = fields.get("InstanceName", "MSSQLSERVER")
            out.append(fields["ServerName"] if instance.upper() == "MSSQLSERVER" else f"{fields['ServerName']}\\{instance}")
    return out


def from_browser(wait: float = 2.0) -> list[str]:
    """Ask the local network's SQL Server Browsers, as `sqlcmd -L` does."""
    found: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(wait)
        try:
            s.sendto(b"\x02", ("255.255.255.255", 1434))
            while True:
                found += parse_browser(s.recvfrom(65535)[0])
        except OSError:
            pass
    return found


def parse_spns(text: str) -> list[str]:
    """`setspn -Q MSSQLSvc/*`: MSSQLSvc/host.domain:1433 and MSSQLSvc/host.domain:INSTANCE."""
    out = []
    for host, port in re.findall(r"MSSQLSvc/([^\s:]+)(?::(\S+))?", text, re.I):
        if port and not port.isdigit():
            out.append(f"{host}\\{port}")
        elif port and port != "1433":
            out.append(f"{host},{port}")
        else:
            out.append(host)
    return out


def from_domain() -> list[str]:
    """SQL Servers with a service name in Active Directory."""
    setspn = shutil.which("setspn")
    if setspn is None:
        return []
    try:
        return parse_spns(subprocess.run([setspn, "-Q", "MSSQLSvc/*"], capture_output=True, text=True, timeout=120).stdout)
    except (OSError, subprocess.SubprocessError):
        return []


# ------------------------------------------------------------------------------------- SQL Server: trying each --

def _why(error: str) -> str:
    text = error.lower()
    if "login failed" in text or "cannot open database" in text:
        return "your login was refused"
    if "odbc driver" in text or "libodbc" in text or "im002" in text:
        return "no SQL Server ODBC driver on this machine: install ODBC Driver 18 for SQL Server"
    if "timeout" in text or "could not open a connection" in text or "network" in text or "08001" in text:
        return "it did not answer"
    return error[:200]


def probe_sql(t: Target, config: str | None = None) -> Target:
    """Can this login read its jobs? Tries a verified certificate first, and says if it had to trust one."""
    doc = ask(t.args(config), "jobs", "--brief")
    if not doc.get("ok") and not t.env and _CERTIFICATE.search(str(doc.get("error"))):
        t.trust = True
        doc = ask(t.args(), "jobs", "--brief")
    if not doc.get("ok"):
        t.can, t.why_not = [], _why(str(doc.get("error")))
        return t
    t.size = len(doc.get("jobs") or [])
    t.can, t.why_not = ["SQL Agent jobs"], None
    info = ask(t.args(config), "inspect")
    if info.get("ok") and info.get("has_ssisdb"):
        t.can.append("SSIS catalog")
    return t


def find_sql(cfg: Config, network: bool, named: list[str]) -> list[Target]:
    found: dict[str, Target] = {}

    def add(server: str, how: str, env: str | None = None) -> None:
        if _NOT_SQL_AGENT.search(server):
            return
        t = found.setdefault(server.lower(), Target("sql_server", server, env=env))
        t.env = t.env or env
        if how not in t.found_by:
            t.found_by.append(how)
    for s in named:
        add(s, "asked for")
    for s in from_environment():
        add(s, "LOADWATCH_SERVERS")
    for env, s in from_ssisbliss(cfg.ssisbliss_config).items():
        add(s, "ssisbliss.toml", env)
    for s in local_instances():
        add(s, "this machine")
    for s in from_tools():
        add(s, "your SQL tools")
    if network:
        for s in from_domain():
            add(s, "the domain")
        for s in from_browser():
            add(s, "the network")
    return list(found.values())


# -------------------------------------------------------------------------------------------------- Fabric --

def find_fabric(client: FabricClient) -> tuple[list[Target], list[str]]:
    try:
        workspaces = client.workspaces()
    except ValueError as e:
        return [], [f"Fabric was not looked at: {e}"]
    except Exception as e:  # noqa: BLE001 - one service being down must not end discovery
        return [], [f"Fabric could not be read: {type(e).__name__}: {e}"]

    def one(w: dict) -> Target:
        t = Target("fabric_workspace", w["displayName"], ["your az login"], id=w["id"])
        try:
            items = watchable(client.items(w["id"]))
        except Exception as e:  # noqa: BLE001 - reported on the workspace
            t.why_not = str(e)
            return t
        t.size = len(items)
        kinds = sorted({"semantic model refreshes" if i["type"] == "SemanticModel" else "scheduled items" for i in items})
        t.can = kinds
        t.why_not = None if items else "nothing in it runs"
        return t
    with ThreadPoolExecutor(PROBES) as pool:
        return list(pool.map(one, [w for w in workspaces if w.get("type") != "AdminWorkspace"])), []


# ---------------------------------------------------------------------------------------------- the estate --

def _path(cfg: Config) -> Path:
    return cfg.db.parent / "estate.json"


def save(cfg: Config, estate: Estate) -> None:
    path = _path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(estate), indent=1))


def load(cfg: Config) -> Estate | None:
    try:
        doc = json.loads(_path(cfg).read_text())
        return Estate(doc["at"], [Target(**t) for t in doc["targets"]], doc.get("notes", []))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def merge(found: list[Target], before: Estate | None) -> list[Target]:
    """What could be watched and now cannot stays watched: its next pass says so, where a quiet removal would say nothing."""
    out = {(t.kind, t.name.lower()): t for t in found}
    for old in (before.targets if before else []):
        key = (old.kind, old.name.lower())
        new = out.get(key)
        if old.watched and (new is None or not new.watched):
            kept = out[key] = old
            if new is not None:
                kept.why_not = new.why_not
    return sorted(out.values(), key=lambda t: (t.kind, not t.watched, t.name.lower()))


def discover(cfg: Config, now: datetime, network: bool = False, named: list[str] = (), client: FabricClient | None = None) -> Estate:
    before = load(cfg)
    sql = find_sql(cfg, network, list(named))
    with ThreadPoolExecutor(PROBES) as pool:
        sql = list(pool.map(lambda t: probe_sql(t, cfg.ssisbliss_config), sql))
    fabric, notes = find_fabric(client or fabric_client())
    for t in sql:
        if t.watched and t.trust:
            notes.append(f"{t.name}: its certificate could not be verified, so it is trusted for reading")
    if not sql:
        notes.append("No SQL Server was found on this machine or in your SQL tools. Run `loadwatch discover --network`, or "
                     "`loadwatch discover --server NAME`")
    estate = Estate(now.isoformat()[:19], merge([*sql, *fabric], before), notes)
    save(cfg, estate)
    return estate


def current(cfg: Config, now: datetime, refresh: bool = False, client: FabricClient | None = None) -> Estate:
    """What to watch: what loadwatch.toml lists, or else the estate, found again when it is a day old."""
    if cfg.environments:
        return Estate(now.isoformat()[:19], [Target("sql_server", e, ["loadwatch.toml"], ["SQL Agent jobs"], env=e) for e in cfg.environments])
    estate = load(cfg)
    if estate is None or refresh or now - datetime.fromisoformat(estate.at) >= MAX_AGE:
        named = [t.name for t in estate.targets if "asked for" in t.found_by or "the domain" in t.found_by
                 or "the network" in t.found_by] if estate else []
        return discover(cfg, now, named=named, client=client)
    return estate
