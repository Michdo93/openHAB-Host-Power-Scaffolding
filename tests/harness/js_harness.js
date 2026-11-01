// Runs a generated host_power.js with a mocked openhab-js API and prints a JSON trace.
//   node js_harness.js <host_power.js> <events.json> [--exec]
// events.json: [{"item": "Pwr_x_Power", "command": "ON"}, ...]
// --exec: really run the shutdown argv (see harness_exec in common.sh) instead of only recording it.
'use strict';
const fs = require('fs');
const vm = require('vm');
const { spawnSync } = require('child_process');

const [file, eventsFile, flag] = process.argv.slice(2);
const trace = { rules: [], calls: [], logs: [] };
const execRunner = process.env.HARNESS_EXEC_RUNNER;   // e.g. "runuser -u openhab --"

const sandbox = {
  console: {
    info: (m) => trace.logs.push(['info', String(m)]),
    warn: (m) => trace.logs.push(['warn', String(m)]),
    error: (m) => trace.logs.push(['error', String(m)])
  },
  time: { Duration: { ofSeconds: (s) => ({ seconds: s }) } },
  triggers: { ItemCommandTrigger: (item, cmd) => ({ type: 'ItemCommandTrigger', item, cmd }) },
  rules: { JSRule: (cfg) => trace.rules.push(cfg) },
  actions: {
    thingActions: (binding, uid) => (uid.startsWith('missing') ? null : {
      sendWakeOnLanPacketViaMac: () => trace.calls.push({ wol: uid, binding })
    }),
    Exec: {
      executeCommandLine: (timeout, ...argv) => {
        const call = { exec: argv, timeout: timeout.seconds };
        if (flag === '--exec') {
          const runner = execRunner ? execRunner.split(' ') : [];
          const res = spawnSync(runner[0] || argv[0], [...runner.slice(1), ...(runner.length ? argv : argv.slice(1))],
            { encoding: 'utf8', timeout: timeout.seconds * 1000 });
          call.rc = res.status;
          call.output = (res.stdout + res.stderr).trim();
        }
        trace.calls.push(call);
        return call.output || '';
      }
    }
  }
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: file });

const events = JSON.parse(fs.readFileSync(eventsFile, 'utf8'));
for (const rule of trace.rules) {
  for (const ev of events) {
    const triggered = rule.triggers.some((t) => t.item === ev.item && (!t.cmd || t.cmd === ev.command));
    if (triggered) rule.execute({ itemName: ev.item, receivedCommand: ev.command });
  }
}
trace.rules = trace.rules.map((r) => ({ name: r.name, id: r.id, triggers: r.triggers }));
process.stdout.write(JSON.stringify(trace));
