"""Emulate the Exec Binding for the generated reboot Things.

    python3 exec_thing.py <out-dir> [--run]

For every exec:command Thing: check the whitelist exactly like ExecWhitelistWatchService
(raw string, '#' lines ignored), apply java.util.Formatter semantics for '%%', and with
--run execute it via `sh -c` (as the binding does), prefixed with $HARNESS_EXEC_RUNNER.
Prints one JSON object per Thing.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

out = Path(sys.argv[1])
run = "--run" in sys.argv[2:]
things = (out / "things/host_power.things").read_text(encoding="utf-8")
whitelist = {l for l in (out / "misc/exec.whitelist").read_text(encoding="utf-8").splitlines()
             if not l.strip().startswith("#")}

for m in re.finditer(r'^Thing (exec:command:\S+) .*?\[ command="((?:[^"\\]|\\.)*)"', things, re.M):
    uid, raw = m.group(1), re.sub(r'\\(.)', r'\1', m.group(2))
    rec = {"uid": uid, "whitelisted": raw in whitelist}
    cmd = raw.replace("%%", "%")      # String.format(commandLine, date) with no other placeholders
    if run and rec["whitelisted"]:
        runner = os.environ.get("HARNESS_EXEC_RUNNER", "").split()
        res = subprocess.run([*runner, "sh", "-c", cmd], capture_output=True, text=True, timeout=30)
        rec["rc"], rec["output"] = res.returncode, (res.stdout + res.stderr).strip()
    print(json.dumps(rec))
