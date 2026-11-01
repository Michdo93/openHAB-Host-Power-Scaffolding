"""Load hosts.yaml + overrides.yaml into the model the templates render from."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import yaml

DEFAULT_SETTINGS = {
    "item_prefix": "Pwr_",            # all item names start with this
    "thing_prefix": "pwr_",           # UID segment of the generated Things
    "location": "Netzwerk",           # Thing location
    "ssh_key": "/var/lib/openhab/.ssh/id_ed25519",   # private key of the openhab user
    "ssh_connect_timeout": 5,         # seconds
    "command_timeout": 20,            # seconds (Exec Thing and executeCommandLine)
    "ping_refresh": 30000,            # ms, refreshInterval of the ping Things
    "ping_things": "own",             # own | external
    "external_ping_uid": "network:pingdevice:{id}",  # used with ping_things: external
    "rule_uid": "host_power_command",
}

HOST_FIELDS = {"name", "id", "label", "address", "port", "os", "ssh_user", "mac", "interface",
               "wol_supported", "wol_enabled", "poweroff", "reboot", "enabled", "location"}

_ID_INVALID = re.compile(r"[^a-z0-9_]")
_MAC = re.compile(r"^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$")
_ADDRESS = re.compile(r"^[A-Za-z0-9.\-:]+$")
_USER = re.compile(r"^[A-Za-z0-9_.\-\\]+\$?$")


class ModelError(ValueError):
    pass


def sanitize(name: str) -> str:
    s = name.strip().lower().replace("-", "_").replace(".", "_")
    s = re.sub(r"_+", "_", _ID_INVALID.sub("", s)).strip("_")
    return s if s and s[0].isalpha() else f"h_{s}".rstrip("_")


def _read_yaml(path: Path | None) -> dict:
    if not path:
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ModelError(f"{path}: YAML-Mapping erwartet")
    return data


def ssh_options(settings: dict) -> list[str]:
    """Options shared by all SSH calls: never prompt, verify host keys, use only our key."""
    return ["-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={int(settings['ssh_connect_timeout'])}",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "IdentitiesOnly=yes"]


def ssh_argv(settings: dict, h: dict, remote: str) -> list[str]:
    """Argument vector for one remote command (no shell involved)."""
    return ["ssh", *ssh_options(settings), "-i", settings["ssh_key"], "-p", str(h["port"]),
            h["ssh_target"], remote]


def exec_command_line(argv: list[str]) -> str:
    """Command line for an Exec Thing.

    The Exec Binding runs it with ``sh -c`` and formats it with java.util.Formatter
    first, so every ``%`` must be doubled. The whitelist compares the raw string.
    """
    return " ".join(shlex.quote(a) for a in argv).replace("%", "%%")


def _host(raw: dict, override: dict, settings: dict) -> dict:
    h = {**raw, **override}
    unknown = set(h) - HOST_FIELDS
    if unknown:
        raise ModelError(f"Host {h.get('name')}: unbekannte Felder {sorted(unknown)}")
    name = str(h.get("name") or "").strip()
    if not name:
        raise ModelError("Host ohne 'name'")
    h["name"] = name
    h["id"] = sanitize(str(h.get("id") or name))
    h["label"] = str(h.get("label") or name)
    h["enabled"] = bool(h.get("enabled", True))
    h["os"] = str(h.get("os") or "linux").lower()
    h["port"] = int(h.get("port") or 22)
    h["location"] = h.get("location") or settings["location"]

    addr = str(h.get("address") or "")
    if not _ADDRESS.match(addr):
        raise ModelError(f"Host {name}: ungültige address {addr!r}")
    h["address"] = addr

    mac = h.get("mac")
    if mac:
        mac = str(mac).lower()
        if not _MAC.match(mac) or mac.replace("-", ":") == "00:00:00:00:00:00":
            raise ModelError(f"Host {name}: ungültige MAC {mac!r}")
    h["mac"] = mac or None

    user = h.get("ssh_user")
    if user is not None and not _USER.match(str(user)):
        raise ModelError(f"Host {name}: ungültiger ssh_user {user!r}")
    for key in ("poweroff", "reboot"):
        cmd = h.get(key)
        if cmd is not None and (not str(cmd).strip() or "\n" in str(cmd)):
            raise ModelError(f"Host {name}: {key} muss eine einzeilige Befehlszeile sein")

    h["ssh_target"] = f"{user}@{addr}" if user else None

    # features
    h["can_wake"] = h["mac"] is not None
    h["can_shutdown"] = bool(user and h.get("poweroff"))
    h["can_reboot"] = bool(user and h.get("reboot"))

    # derived names
    ip, tp = settings["item_prefix"], settings["thing_prefix"]
    h["item"] = f"{ip}{h['id']}"
    h["power_item"] = f"{h['item']}_Power"
    h["ping_own"] = settings["ping_things"] == "own"
    h["ping_uid"] = (f"network:pingdevice:{tp}{h['id']}" if h["ping_own"]
                     else settings["external_ping_uid"].format(id=h["id"], name=name))
    h["exec_uid"] = f"exec:command:{tp}{h['id']}_reboot"
    if h["can_shutdown"]:
        h["shutdown_argv"] = ssh_argv(settings, h, str(h["poweroff"]))
    if h["can_reboot"]:
        h["reboot_argv"] = ssh_argv(settings, h, str(h["reboot"]))
        h["reboot_command"] = exec_command_line(h["reboot_argv"])
    return h


def load(hosts_path: Path, overrides_path: Path | None = None) -> dict:
    data, ov = _read_yaml(hosts_path), _read_yaml(overrides_path)
    settings = {**DEFAULT_SETTINGS, **(data.get("settings") or {}), **(ov.get("settings") or {})}
    unknown = set(settings) - set(DEFAULT_SETTINGS)
    if unknown:
        raise ModelError(f"unbekannte settings {sorted(unknown)}")
    if settings["ping_things"] not in ("own", "external"):
        raise ModelError("settings.ping_things muss 'own' oder 'external' sein")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", settings["item_prefix"]):
        raise ModelError("settings.item_prefix: nur Buchstaben, Ziffern, _ (Beginn mit Buchstabe)")
    if not re.fullmatch(r"[a-z0-9_]*", settings["thing_prefix"]):
        raise ModelError("settings.thing_prefix: nur a-z, 0-9, _")
    if not re.fullmatch(r"[^\s'\"%]+", str(settings["ssh_key"])):
        raise ModelError("settings.ssh_key: Pfad ohne Leerzeichen/Anführungszeichen")

    overrides = ov.get("hosts") or {}
    if not isinstance(overrides, dict):
        raise ModelError("overrides.yaml: 'hosts' muss ein Mapping <name>: {...} sein")
    raw_hosts = data.get("hosts") or []
    names = {str(r.get("name")) for r in raw_hosts}
    for extra, val in overrides.items():          # hosts only defined in overrides.yaml
        if extra not in names and isinstance(val, dict) and val.get("address"):
            raw_hosts = [*raw_hosts, {"name": extra}]

    hosts, seen = [], {}
    for raw in raw_hosts:
        h = _host(raw, overrides.get(str(raw.get("name"))) or {}, settings)
        if h["id"] in seen:
            raise ModelError(f"doppelte id {h['id']} ({seen[h['id']]} / {h['name']}) – in overrides.yaml 'id' setzen")
        seen[h["id"]] = h["name"]
        hosts.append(h)
    hosts.sort(key=lambda x: x["id"])
    return {"settings": settings, "hosts": hosts, "active": [h for h in hosts if h["enabled"]],
            "ssh_options": ssh_options(settings)}
