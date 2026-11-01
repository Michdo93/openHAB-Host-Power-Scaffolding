# openHAB Host Power Scaffolding

Switch the computers in your network on, off and restart them from openHAB – with one
Switch per host that also shows whether the host is actually running.

* **ON** → Wake-on-LAN (Network Binding thing action)
* **OFF** → shutdown via SSH (Exec action `executeCommandLine`)
* **Reboot** → Exec Binding Thing, one per host
* The ping of the Network Binding keeps the Switch in sync when a host is switched on or off
  by hand – without triggering Wake-on-LAN or a shutdown.

The repository **generates** Things, Items, the `exec.whitelist` entries and the rule – in
**Rules DSL**, **JavaScript (GraalJS)** or **Python (GraalPy)** – from Jinja2 templates.
Nothing is written into openHAB; you copy the files yourself.

## How the pieces fit together

```
Once, by Ansible (root on the targets via become)
  ansible/power_access.yml       account "ohpower" + sudoers for exactly poweroff/reboot
  ansible-ssh-key-distribution   openhab's public key -> ohpower, host keys -> known_hosts
  ansible/export_hosts.yml       address, MAC, OS, power commands -> hosts.yaml

Generate
  hosts.yaml (+ overrides.yaml) -> generate.py -> out/  (copy to $OPENHAB_CONF yourself)

At runtime, by openHAB (plain SSH key, no root, no Vault password)
  Power  command ON   -> Wake-on-LAN (ping Thing action)
  Power  command OFF  -> ssh ohpower@host sudo -n <poweroff>
  Reboot ON           -> Exec Thing: ssh ohpower@host sudo -n <reboot>
  ping                -> postUpdate of the Power item (never triggers the rule)
```

**Why not call Ansible from openHAB?** Ansible needs the Vault/become password, i.e. root on
every host. Putting that on the openHAB host would turn openHAB into a key to the whole
network. Here Ansible only prepares the targets; openHAB later uses a plain SSH key whose
account may run two commands and nothing else.

**Is the public key enough?** For the login, yes: [ansible-ssh-key-distribution](https://github.com/Michdo93/ansible-ssh-key-distribution)
puts the `openhab` user's public key into `authorized_keys` on the targets and their host keys
into openhab's `known_hosts`, so `ssh -o BatchMode=yes` works without password or prompt.
openHAB does not have to probe IP addresses: every host's address comes from the inventory.
What the key does **not** give is root – that is what `power_access.yml` adds via a sudoers
drop-in limited to the two power commands.

## Requirements

* Control node: `ansible-core`, an inventory with `become` on all targets (e.g. from
  [ansible-inventory-generator](https://github.com/Michdo93/ansible-inventory-generator)),
  a checkout of [ansible-ssh-key-distribution](https://github.com/Michdo93/ansible-ssh-key-distribution)
* Generator: Python ≥ 3.9 with `Jinja2` and `PyYAML` (`pip install -r requirements.txt`)
* openHAB 4.x/5.x with the **Network Binding**, the **Exec Binding** and, depending on the rule
  language, **JavaScript Scripting** or **Python Scripting**; `ssh` installed on the openHAB host
* Targets: OpenSSH server; Wake-on-LAN enabled in firmware and NIC (see below)

## Usage

Replace `openhab-vm` with the inventory name of your openHAB host. Exclude it from the targets,
unless openHAB should be able to switch off its own host.

```bash
export ANSIBLE_CONFIG=/path/to/inventory/ansible.cfg

# 1. dedicated account "ohpower" with sudo rights for poweroff/reboot only
ansible-playbook ansible/power_access.yml -e 'target_hosts=all:!openhab-vm'

# 2. openhab's key pair -> ohpower on all targets (+ known_hosts), only accepted from openhab-vm
ansible-playbook /path/to/ansible-ssh-key-distribution/distribute.yml \
  -e source_host=openhab-vm -e source_user=openhab \
  -e 'target_hosts=all:!openhab-vm' -e target_user=ohpower -e restrict_from=true

# 3. export address, MAC, OS and the exact power commands -> hosts.yaml
ansible-playbook ansible/export_hosts.yml -e 'target_hosts=all:!openhab-vm'

# 4. generate (choose ONE rule language)
python3 generate.py list
python3 generate.py render --lang js        # or: --lang python | --lang dsl
```

`out/` mirrors `$OPENHAB_CONF`:

| Generated file | Copy to |
|---|---|
| `out/things/host_power.things` | `things/` |
| `out/items/host_power.items` | `items/` |
| `out/misc/exec.whitelist` | **append** to `misc/exec.whitelist` |
| `out/rules/host_power.rules` (dsl) | `rules/` |
| `out/automation/js/host_power.js` (js) | `automation/js/` |
| `out/automation/python/host_power.py` (python) | `automation/python/` |

Deploy only one rule file; `render` removes the files of the other languages from `out/`.
`render` validates the output (see [Tests](#tests)); `generate.py validate` re-checks `out/`.

Re-run step 3 and 4 when hosts change. Steps 1 and 2 are idempotent.

## Generated model

Per host (`Pwr_` prefix, id derived from the inventory name, e.g. `lab-pc` → `lab_pc`):

| Item | Type | Linked to | Purpose |
|---|---|---|---|
| `Pwr_<id>` | Group `["Computer"]` | – | equipment |
| `Pwr_<id>_Power` | Switch `["Switch","Power"]` | `network:pingdevice:pwr_<id>:online`, `autoupdate="false"` | state from ping; command ON/OFF → rule |
| `Pwr_<id>_LastSeen` | DateTime | `…:lastseen` | |
| `Pwr_<id>_Reboot` | Switch `["Control"]` | `exec:command:pwr_<id>_reboot:run`, `autoupdate="false"` | ON runs the reboot; the binding sets it back to OFF when done |
| `Pwr_<id>_RebootExit` / `_RebootOutput` | Number / String | `…:exit` / `…:output` | result of the last reboot command |

### The three-way switch

The rule triggers on **received command** only:

| Event | Source | Result |
|---|---|---|
| user/UI/voice sends ON | command | Wake-on-LAN |
| user/UI/voice sends OFF | command | `ssh … sudo -n <poweroff>` |
| host switched on/off by hand, ping notices | `postUpdate` from the binding | item state changes, rule does **not** run |
| openHAB restarts, persistence restores the state | `postUpdate` | rule does not run |

`autoupdate="false"` keeps the Switch at the real state: after OFF it stays ON until the
ping reports the host offline, so the UI never claims a host is off that is still running.

**Do not put the Power switches into a common `Group:Switch`.** A command to a group is
forwarded to every member, so `gAllPCs.sendCommand(OFF)` would shut down every host. The
generated groups mix item types and therefore accept no ON/OFF; the validator rejects a group
that would forward ON/OFF to Power switches. For the same reason the rule uses one trigger
per item instead of `Member of`.

## Configuration

`hosts.yaml` is overwritten by every export. Put manual changes into `overrides.yaml`:

```yaml
settings:
  ssh_key: /var/lib/openhab/.ssh/id_ed25519   # key distributed in step 2
  item_prefix: Pwr_
  ping_refresh: 30000                          # ms
hosts:
  lab-pc:
    label: "Labor-PC"
  nas:
    enabled: false                             # leave out completely
  media-pc:                                    # host that is not in Ansible
    address: 192.168.1.70
    mac: "aa:bb:cc:dd:ee:ff"                   # Wake-on-LAN + ping only, no SSH
```

| Setting | Default | Meaning |
|---|---|---|
| `item_prefix` | `Pwr_` | prefix of all item names |
| `thing_prefix` | `pwr_` | UID segment of the generated Things |
| `location` | `Netzwerk` | Thing location (per host: `location`) |
| `ssh_key` | `/var/lib/openhab/.ssh/id_ed25519` | private key of the `openhab` user (Docker: path inside the container) |
| `ssh_connect_timeout` | `5` | seconds |
| `command_timeout` | `20` | seconds, Exec Thing and `executeCommandLine` |
| `ping_refresh` | `30000` | ms, `refreshInterval` of the ping Things |
| `ping_things` | `own` | `external`: use ping Things defined elsewhere (e.g. by [openHAB-Network-Binding-Scaffolding](https://github.com/Michdo93/openHAB-Network-Binding-Scaffolding)); they need `macAddress` |
| `external_ping_uid` | `network:pingdevice:{id}` | UID pattern for `external` |
| `rule_uid` | `host_power_command` | UID of the generated rule |

Host fields (export or override): `name`, `id`, `label`, `enabled`, `location`, `address`,
`port`, `os`, `ssh_user`, `mac`, `poweroff`, `reboot`. A host without `mac` gets no
Wake-on-LAN, a host without `ssh_user`/`poweroff`/`reboot` no shutdown/reboot – the rule logs
a warning instead.

Without Ansible, write `hosts.yaml` by hand in the same format (see `tests/fixtures/hosts.yaml`).

### Playbook variables

| Variable | Default | Playbooks |
|---|---|---|
| `target_hosts` | `all` | all |
| `power_user` | `ohpower` | all |
| `create_power_user` | `true` | `power_access.yml`; `false` to use an existing account, e.g. `-e power_user=sysadmin` |
| `power_user_shell` | `/bin/sh` | `power_access.yml` |
| `sudoers_file` | `/etc/sudoers.d/openhab-power` | `power_access.yml`, `revoke_power_access.yml` |
| `remove_power_user` | `false` | `revoke_power_access.yml` |
| `power_user_windows` | `ansible_user` | `export_hosts.yml` |
| `ssh_port` | `ansible_port` (SSH connections) / `22` | `export_hosts.yml` |
| `export_file` | `<repo>/hosts.yaml` | `export_hosts.yml` |

All of them also work per host in `host_vars`/`group_vars`.

The sudoers drop-in written by `power_access.yml` (validated with `visudo -cf`):

```
Defaults:ohpower !requiretty
ohpower ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl reboot
```

`systemctl` is used when systemd is the running init system, otherwise `shutdown -h/-r now`.
`export_hosts.yml` writes exactly these command lines into `hosts.yaml`, so sudo matches.

## Wake-on-LAN

`export_hosts.yml` takes the MAC of the interface that carries `ansible_host` (fallback:
default route) and reads `ethtool` – `generate.py list` shows *WoL laut ethtool aus* when the
NIC supports it but has it disabled. Enable it in the BIOS/UEFI and on the NIC, e.g. with
NetworkManager:

```bash
nmcli connection modify "<connection>" 802-3-ethernet.wake-on-lan magic
```

The magic packet is a broadcast: openHAB and the target must be in the same layer-2 network.

## Windows and macOS

`power_access.yml` skips them. `export_hosts.yml` writes `shutdown /s /t 0` / `shutdown /r /t 0`
for Windows (OpenSSH Server, account from `power_user_windows`, which needs the right to shut
down – usually an administrator) and `sudo -n /sbin/shutdown -h|-r now` for macOS (add the
sudoers entry yourself). Both paths are untested.

## Security

* openHAB's key opens exactly one account per host, which may run exactly two commands as root.
* Use `restrict_from=true` in step 2 so the key is only accepted from the openHAB host.
* `StrictHostKeyChecking=yes`: openHAB refuses hosts whose key is not in `known_hosts` –
  the key distribution writes them from Ansible facts.
* Undo: `ansible-playbook ansible/revoke_power_access.yml [-e remove_power_user=true]` and
  `revoke.yml` of ansible-ssh-key-distribution.

## Tests

* `pytest` – model, rendering of all three languages, validator, and the generated JavaScript
  and Python rules executed against mocked openhab-js / openhab-python APIs
  (`tests/harness/`).
* `sudo bash tests/e2e/lab.sh` – the whole chain on one machine: two sshd as targets, an `openhab`
  system account, `power_access.yml` (incl. idempotence), `distribute.yml` of
  ansible-ssh-key-distribution, `export_hosts.yml`, all three generators; the JS/Python rules
  and the Exec Things (whitelist check + `sh -c`, as the binding does) run their SSH commands
  as `openhab` against the targets. Instead of powering off, the commands are overridden with
  `sudo -n -l <cmd>`, which only succeeds if sudoers allows exactly that command. Negative
  checks: other sudo commands, login to other accounts, and the commands after revoke are refused.
* CI runs both, plus `ansible-lint`.

Not covered: a live openHAB instance (the Rules DSL file is only checked structurally) and a
real Wake-on-LAN packet.

## Repository layout

```text
openHAB-Host-Power-Scaffolding/
├── ansible/
│   ├── power_access.yml           account + sudoers drop-in on Linux targets
│   ├── export_hosts.yml           facts -> hosts.yaml
│   ├── revoke_power_access.yml    undo power_access.yml
│   └── tasks/linux_power_commands.yml
├── ohpower/                       generator (model, render, validate)
│   └── templates/                 host_power.things/.items/.rules/.js/.py.j2, exec.whitelist.j2
├── generate.py                    CLI: render | validate | list
└── tests/                         pytest, harnesses, e2e/lab.sh
```
