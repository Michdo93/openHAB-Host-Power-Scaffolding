#!/usr/bin/env bash
# End-to-end test on one Linux machine (root required; used by CI).
#
# Simulates an Ansible control node, an openHAB host and two targets (two sshd on
# 127.0.0.2:2201 and 127.0.0.3:2202), then runs the real chain:
#   power_access.yml -> ansible-ssh-key-distribution/distribute.yml -> export_hosts.yml
#   -> generate.py (js, python, dsl) -> rules executed with mocked openHAB APIs,
#      their SSH commands executed as the openhab user against the targets.
# Instead of powering anything off, the commands are overridden with "sudo -n -l <cmd>",
# which only succeeds if exactly that command is allowed by the sudoers drop-in.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="${WORK:-$(mktemp -d)}"
KEYDIST="${KEYDIST:-$WORK/ansible-ssh-key-distribution}"
export ANSIBLE_FORKS=1                     # both "targets" are the same machine
cd "$WORK"

step() { printf '\n=== %s\n' "$*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

step "Lab: accounts and sshd"
id sysadmin >/dev/null 2>&1 || useradd -m -s /bin/bash sysadmin
usermod -aG sudo sysadmin 2>/dev/null || usermod -aG wheel sysadmin
echo 'sysadmin:sysadmin-pw' | chpasswd
id openhab >/dev/null 2>&1 || useradd -r -m -d /var/lib/openhab -s /usr/sbin/nologin openhab
[ -f "$WORK/control_key" ] || ssh-keygen -q -t ed25519 -N '' -f "$WORK/control_key"
install -d -m700 -o sysadmin -g sysadmin /home/sysadmin/.ssh
install -m600 -o sysadmin -g sysadmin "$WORK/control_key.pub" /home/sysadmin/.ssh/authorized_keys
ssh-keygen -A >/dev/null; mkdir -p /run/sshd
for p in "127.0.0.2 2201" "127.0.0.3 2202"; do
  set -- $p
  pgrep -f "sshd.*-p $2" >/dev/null || /usr/sbin/sshd -p "$2" -o ListenAddress="$1" -o PidFile="/run/sshd-$2.pid"
done

cat > inventory.ini <<EOF
[openhab]
openhab-vm ansible_host=127.0.0.1 ansible_connection=local

[lab]
lab-pc   ansible_host=127.0.0.2 ansible_port=2201
lab-nas  ansible_host=127.0.0.3 ansible_port=2202

[all:vars]
ansible_user=sysadmin
ansible_become_password=sysadmin-pw
ansible_ssh_private_key_file=$WORK/control_key
ansible_ssh_common_args=-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$WORK/known_hosts
ansible_python_interpreter=/usr/bin/python3
EOF

step "power_access.yml (dedicated account + sudoers)"
ansible-playbook -i inventory.ini "$REPO/ansible/power_access.yml" -e target_hosts=lab
step "power_access.yml again (must be idempotent)"
ansible-playbook -i inventory.ini "$REPO/ansible/power_access.yml" -e target_hosts=lab | tee pa2.log
grep -Eq 'lab-pc .*changed=0' pa2.log && grep -Eq 'lab-nas .*changed=0' pa2.log || fail "not idempotent"

step "ansible-ssh-key-distribution: openhab's key -> ohpower on the targets"
[ -d "$KEYDIST" ] || git clone -q --depth 1 https://github.com/Michdo93/ansible-ssh-key-distribution.git "$KEYDIST"
ansible-playbook -i inventory.ini "$KEYDIST/distribute.yml" -e source_host=openhab-vm -e source_user=openhab \
  -e target_hosts=lab -e target_user=ohpower

step "export_hosts.yml"
ansible-playbook -i inventory.ini "$REPO/ansible/export_hosts.yml" -e target_hosts=lab -e export_file="$WORK/hosts.yaml"
cat hosts.yaml

cat > overrides.yaml <<'EOF'
hosts:
  lab-pc:  {poweroff: "sudo -n -l /usr/sbin/shutdown -h now", reboot: "sudo -n -l /usr/sbin/shutdown -r now"}
  lab-nas: {poweroff: "sudo -n -l /usr/sbin/shutdown -h now", reboot: "sudo -n -l /usr/sbin/shutdown -r now"}
EOF
# Without systemd as init the playbooks choose /usr/sbin/shutdown; on a systemd host use systemctl.
if [ -d /run/systemd/system ]; then sed -i 's#/usr/sbin/shutdown -h now#/usr/bin/systemctl poweroff#; s#/usr/sbin/shutdown -r now#/usr/bin/systemctl reboot#' overrides.yaml; fi
echo '[{"item":"Pwr_lab_pc_Power","command":"ON"},{"item":"Pwr_lab_pc_Power","command":"OFF"},{"item":"Pwr_lab_nas_Power","command":"OFF"}]' > events.json
export HARNESS_EXEC_RUNNER="runuser -u openhab --"

check_trace() {  # stdin: harness JSON
  python3 -c '
import json, sys
t = json.load(sys.stdin)
wol = [c["wol"] for c in t["calls"] if "wol" in c]
ex = [c for c in t["calls"] if "exec" in c]
assert wol == ["network:pingdevice:pwr_lab_pc"], wol
assert len(ex) == 2 and all(c["rc"] == 0 for c in ex), ex
assert all("shutdown -h now" in c["output"] or "systemctl poweroff" in c["output"] for c in ex), ex
print("  WoL:", wol[0]); [print("  SSH:", c["exec"][-2], "->", c["output"]) for c in ex]'
}

for lang in js python dsl; do
  step "generate.py render --lang $lang"
  python3 "$REPO/generate.py" -i hosts.yaml --overrides overrides.yaml -o "out-$lang" render --lang "$lang"
done

step "JavaScript rule (mocked openhab-js, real SSH as openhab)"
node "$REPO/tests/harness/js_harness.js" out-js/automation/js/host_power.js events.json --exec | check_trace
step "Python rule (mocked openhab-python, real SSH as openhab)"
python3 "$REPO/tests/harness/py_harness.py" out-python/automation/python/host_power.py events.json --exec | check_trace

step "Reboot Exec Things (whitelist check + sh -c, as the binding does)"
python3 "$REPO/tests/harness/exec_thing.py" out-dsl --run | tee exec.json
python3 -c '
import json
recs = [json.loads(l) for l in open("exec.json")]
assert len(recs) == 2 and all(r["whitelisted"] and r["rc"] == 0 for r in recs), recs'

step "Negative checks"
SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -i /var/lib/openhab/.ssh/id_ed25519 -p 2201)
runuser -u openhab -- "${SSH[@]}" ohpower@127.0.0.2 'sudo -n /usr/bin/id' 2>/dev/null && fail "ohpower may run id as root"
runuser -u openhab -- "${SSH[@]}" sysadmin@127.0.0.2 true 2>/dev/null && fail "key accepted for sysadmin"
echo "  sudo id: denied, login as sysadmin: denied"

step "revoke_power_access.yml"
ansible-playbook -i inventory.ini "$REPO/ansible/revoke_power_access.yml" -e target_hosts=lab
PWR=$(python3 -c "import yaml; print(yaml.safe_load(open('hosts.yaml'))['hosts'][0]['poweroff'].replace('sudo -n ', 'sudo -n -l ', 1))")
runuser -u openhab -- "${SSH[@]}" ohpower@127.0.0.2 "$PWR" 2>/dev/null && fail "still allowed after revoke: $PWR"
echo "  power commands denied after revoke"

printf '\nALL E2E CHECKS PASSED (%s)\n' "$WORK"
