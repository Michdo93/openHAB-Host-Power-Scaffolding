"""Static checks of the generated files.

* Things: known Thing types, required parameters, Exec commands present in the whitelist
* Items:  channels exist with the right item type, groups defined, no group would fan out
          ON/OFF to several Power switches
* Rules:  every item named in the rule exists and is a Power switch linked to a ping Thing;
          JavaScript is syntax-checked with node, Python with compile(), DSL structurally
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

CHANNELS = {
    "network:pingdevice": {"online": "Switch", "latency": "Number:Time", "lastseen": "DateTime"},
    "exec:command": {"output": "String", "stdout": "String", "stderr": "String", "input": "String",
                     "exit": "Number", "run": "Switch", "lastexecution": "DateTime"},
}
PARAMS = {
    "network:pingdevice": ({"hostname"}, {"hostname", "macAddress", "refreshInterval", "retry", "timeout",
                                          "networkInterfaceNames", "useIOSWakeUp", "useArpPing", "useIcmpPing"}),
    "exec:command": ({"command"}, {"command", "transform", "interval", "timeout", "autorun", "charset"}),
}
# accepted command types per item type (openHAB core), used for the fan-out check
ACCEPTS = {"Switch": {"OnOff", "Refresh"}, "DateTime": {"DateTime", "Refresh"},
           "Number": {"Decimal", "Quantity", "Refresh"}, "String": {"String", "Refresh"}}

_STR = r'"(?:[^"\\]|\\.)*"'
_THING = re.compile(rf'^\s*Thing\s+(\w+:\w+):([\w-]+)\s+(?:{_STR}\s+)?(?:@\s*{_STR}\s+)?\[(.*)\]\s*$')
_PARAM = re.compile(rf'(\w+)\s*=\s*({_STR}|[^,\s\]]+)')
_ITEM = re.compile(r'^\s*(Group|Switch|DateTime|Number(?::\w+)?|String)\s+(\w+)\s*(.*)$')
_CHANNEL = re.compile(r'channel="([\w-]+:[\w-]+):([\w-]+):(\w+)"')


def _unquote(s: str) -> str:
    if s.startswith('"'):
        return re.sub(r'\\(.)', r'\1', s[1:-1])
    return s


def _code(text: str, comment: str = "//"):
    for no, line in enumerate(text.splitlines(), 1):
        if line.strip() and not line.strip().startswith(comment):
            yield no, line


def check_things(things: str, whitelist: str) -> tuple[list[str], dict[str, str]]:
    errors, uids = [], {}
    allowed = {line for line in whitelist.splitlines() if line and not line.strip().startswith("#")}
    for no, line in _code(things):
        m = _THING.match(line)
        if not m:
            errors.append(f"things:{no}: nicht erkannte Thing-Zeile")
            continue
        ttype, tid, body = m.groups()
        uid = f"{ttype}:{tid}"
        if ttype not in PARAMS:
            errors.append(f"things:{no}: unbekannter Thing-Typ {ttype}")
            continue
        if uid in uids:
            errors.append(f"things:{no}: doppelte UID {uid}")
        uids[uid] = ttype
        params = {k: _unquote(v) for k, v in _PARAM.findall(body)}
        required, known = PARAMS[ttype]
        for p in sorted(required - set(params)):
            errors.append(f"things:{no}: Pflichtparameter {p} fehlt ({uid})")
        for p in sorted(set(params) - known):
            errors.append(f"things:{no}: unbekannter Parameter {p} ({uid})")
        if ttype == "exec:command" and params.get("command") not in allowed:
            errors.append(f"things:{no}: command von {uid} steht nicht in exec.whitelist")
        if ttype == "exec:command" and params.get("interval", "0") != "0":
            errors.append(f"things:{no}: {uid} muss interval=0 haben (sonst periodischer Neustart!)")
    return errors, uids


def check_items(items: str, uids: dict[str, str], external_ok: bool) -> tuple[list[str], dict]:
    errors, names, members = [], {}, {}
    for no, line in _code(items):
        m = _ITEM.match(line)
        if not m:
            errors.append(f"items:{no}: nicht erkannte Item-Zeile")
            continue
        itype, name, rest = m.groups()
        if name in names:
            errors.append(f"items:{no}: doppelter Item-Name {name}")
        rest_ns = re.sub(_STR, '""', rest)
        g = re.search(r'\(([\w\s,]+)\)', rest_ns.split("{")[0])
        groups = [x.strip() for x in g.group(1).split(",")] if g else []
        names[name] = {"type": itype, "groups": groups, "channels": _CHANNEL.findall(rest),
                       "line": no, "autoupdate_off": 'autoupdate="false"' in rest}
        for grp in groups:
            members.setdefault(grp, []).append(name)
        for ttype, tid, ch in names[name]["channels"]:
            uid = f"{ttype}:{tid}"
            if uid not in uids and not (external_ok and ttype == "network:pingdevice"):
                errors.append(f"items:{no}: Channel verweist auf fehlendes Thing {uid}")
            expected = CHANNELS.get(ttype, {}).get(ch)
            if expected is None:
                errors.append(f"items:{no}: Channel {ch} existiert nicht für {ttype}")
            elif expected != itype:
                errors.append(f"items:{no}: {name} ist {itype}, Channel {ch} erwartet {expected}")
    for grp, mem in members.items():
        if names.get(grp, {}).get("type") != "Group":
            errors.append(f"items: Gruppe {grp} ist nicht definiert")
            continue

    def accepts(name: str, seen=()) -> set[str]:
        info = names.get(name)
        if info is None or name in seen:
            return set()
        if info["type"] != "Group":
            return ACCEPTS.get(info["type"].split(":")[0], {"Refresh"})
        sets = [accepts(m, (*seen, name)) for m in members.get(name, [])]
        return set.intersection(*sets) if sets else set()

    for grp in members:
        if names.get(grp, {}).get("type") == "Group" and "OnOff" in accepts(grp):
            power = [m for m in members[grp] if m.endswith("_Power")]
            if power:
                errors.append(f"items: Gruppe {grp} würde ON/OFF an {', '.join(power)} weiterleiten")
    return errors, names


def check_rule(lang: str, text: str, items: dict) -> list[str]:
    errors = []
    if lang == "js":
        refs = re.findall(r'^\s*"(\w+)":\s*\{', text, re.M)
        node = shutil.which("node")
        if node:
            res = subprocess.run([node, "--check", "-"], input=text, capture_output=True, text=True)
            if res.returncode:
                errors.append(f"js: Syntaxfehler: {res.stderr.strip().splitlines()[-1] if res.stderr else ''}")
    elif lang == "python":
        refs = re.findall(r'^\s*"(\w+)":\s*\{', text, re.M)
        try:
            compile(text, "host_power.py", "exec")
        except SyntaxError as e:
            errors.append(f"python: Syntaxfehler Zeile {e.lineno}: {e.msg}")
    else:
        refs = re.findall(r'^\s*Item\s+(\w+)\s+received\s+command', text, re.M)
        keys = re.findall(r'^\s*"(\w+)"\s*->', text, re.M)
        if sorted(refs) != sorted(keys):
            errors.append("dsl: Trigger und HOST_POWER-Tabelle passen nicht zusammen")
        code = re.sub(_STR, '""', "\n".join(l for _, l in _code(text)))
        for a, b in (("(", ")"), ("{", "}")):
            if code.count(a) != code.count(b):
                errors.append(f"dsl: unbalancierte Klammern {a}{b}")
        if len(re.findall(r'^rule\s', code, re.M)) != len(re.findall(r'^end\s*$', code, re.M)):
            errors.append("dsl: rule/end passen nicht zusammen")
    for r in refs:
        info = items.get(r)
        if info is None:
            errors.append(f"{lang}: Item {r} ist nicht definiert")
        elif not (info["type"] == "Switch" and any(t == "network:pingdevice" and c == "online"
                                                    for t, _, c in info["channels"])):
            errors.append(f"{lang}: {r} ist kein mit pingdevice:online verknüpfter Switch")
        elif not info["autoupdate_off"]:
            errors.append(f"{lang}: {r} braucht autoupdate=\"false\"")
    return errors


RULE_FILES = {"dsl": "rules/host_power.rules", "js": "automation/js/host_power.js",
              "python": "automation/python/host_power.py"}


def validate_dir(out: Path, external_ping: bool = False) -> list[str]:
    read = lambda rel: (out / rel).read_text(encoding="utf-8")
    errors, uids = check_things(read("things/host_power.things"), read("misc/exec.whitelist"))
    item_errors, items = check_items(read("items/host_power.items"), uids, external_ping)
    errors += item_errors
    langs = [lang for lang, rel in RULE_FILES.items() if (out / rel).exists()]
    if len(langs) != 1:
        errors.append(f"genau eine Regeldatei erwartet, gefunden: {langs or 'keine'}")
    for lang in langs:
        errors += check_rule(lang, read(RULE_FILES[lang]), items)
    return errors
