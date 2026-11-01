"""Run a generated host_power.py with a mocked openhab-python API and print a JSON trace.

    python3 py_harness.py <host_power.py> <events.json> [--exec]

--exec really runs the shutdown argv (prefixed with $HARNESS_EXEC_RUNNER, e.g.
"runuser -u openhab --") instead of only recording it.
"""

import json
import os
import subprocess
import sys
import types

file, events_file = sys.argv[1], sys.argv[2]
do_exec = "--exec" in sys.argv[3:]
trace = {"rules": [], "calls": [], "logs": []}


class Logger:
    def info(self, m): trace["logs"].append(["info", str(m)])
    def warn(self, m): trace["logs"].append(["warn", str(m)])
    def error(self, m): trace["logs"].append(["error", str(m)])


class ItemCommandTrigger:
    def __init__(self, item_name, command=None, trigger_name=None):
        self.item, self.cmd = item_name, command


def rule(name=None, description=None, tags=None, triggers=None, conditions=None, uid=None, **_):
    def deco(cls):
        cls.logger = Logger()
        trace["rules"].append({"name": name, "uid": uid, "obj": cls(), "triggers": triggers or []})
        return cls
    return deco


class Exec:
    @staticmethod
    def executeCommandLine(timeout, *argv):
        call = {"exec": list(argv), "timeout": timeout.total_seconds()}
        if do_exec:
            runner = os.environ.get("HARNESS_EXEC_RUNNER", "").split()
            res = subprocess.run([*runner, *argv], capture_output=True, text=True, timeout=timeout.total_seconds())
            call["rc"], call["output"] = res.returncode, (res.stdout + res.stderr).strip()
        trace["calls"].append(call)
        return call.get("output", "")


class ThingActions:
    def __init__(self, uid): self.uid = uid
    def sendWakeOnLanPacketViaMac(self): trace["calls"].append({"wol": self.uid})


class Actions:
    @staticmethod
    def get(binding, uid):
        return None if uid.startswith("missing") else ThingActions(uid)


class Event:
    def __init__(self, item, command): self.item, self.command = item, command
    def getItemName(self): return self.item
    def getItemCommand(self): return self.command      # str() of a Java OnOffType is "ON"/"OFF"


openhab = types.ModuleType("openhab")
openhab.rule = rule
openhab_triggers = types.ModuleType("openhab.triggers")
openhab_triggers.ItemCommandTrigger = ItemCommandTrigger
openhab_actions = types.ModuleType("openhab.actions")
openhab_actions.Exec = Exec
scope = types.ModuleType("scope")
scope.actions = Actions
sys.modules.update({"openhab": openhab, "openhab.triggers": openhab_triggers,
                    "openhab.actions": openhab_actions, "scope": scope})

code = compile(open(file, encoding="utf-8").read(), file, "exec")
exec(code, {"__name__": "host_power"})

for ev in json.load(open(events_file)):
    for r in trace["rules"]:
        if any(t.item == ev["item"] and (t.cmd is None or t.cmd == ev["command"]) for t in r["triggers"]):
            r["obj"].execute(None, {"event": Event(ev["item"], ev["command"])})

trace["rules"] = [{"name": r["name"], "id": r["uid"], "triggers": [{"item": t.item} for t in r["triggers"]]}
                  for r in trace["rules"]]
print(json.dumps(trace))
