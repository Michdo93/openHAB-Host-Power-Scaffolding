import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ohpower.model import ModelError, exec_command_line, load, sanitize
from ohpower.render import render
from ohpower.validate import check_items, check_things, validate_dir

FIX = Path(__file__).parent / "fixtures"
HARNESS = Path(__file__).parent / "harness"
LANGS = ["dsl", "js", "python"]
RULE = {"dsl": "rules/host_power.rules", "js": "automation/js/host_power.js",
        "python": "automation/python/host_power.py"}


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data, allow_unicode=True))
    return p


@pytest.fixture
def overrides(tmp_path):
    return write(tmp_path, "overrides.yaml", {
        "settings": {"ssh_key": "/var/lib/openhab/.ssh/id_ed25519_power"},
        "hosts": {"lab-pc": {"label": 'Büro-PC "Michael"'}, "old-box": {"enabled": False}},
    })


@pytest.fixture
def model(overrides):
    return load(FIX / "hosts.yaml", overrides)


# ---------- model ----------
def test_sanitize():
    assert sanitize("nas.lab") == "nas_lab" and sanitize("3D-Drucker") == "h_3d_drucker"


def test_features_and_overrides(model):
    by = {h["name"]: h for h in model["hosts"]}
    assert [h["name"] for h in model["active"]] == ["lab-pc", "nas.lab", "printer", "win-pc"]
    assert by["lab-pc"]["label"] == 'Büro-PC "Michael"' and by["lab-pc"]["mac"] == "aa:bb:cc:00:00:20"
    assert by["win-pc"]["can_wake"] is False and by["win-pc"]["can_shutdown"] is True
    assert by["printer"]["can_wake"] is True and not by["printer"]["can_shutdown"] and not by["printer"]["can_reboot"]
    assert by["nas.lab"]["shutdown_argv"][-2:] == ["ohpower@nas.lab.local", "sudo -n /usr/sbin/shutdown -h now"]
    assert by["nas.lab"]["shutdown_argv"][by["nas.lab"]["shutdown_argv"].index("-p") + 1] == "2222"
    assert "-i" in by["lab-pc"]["shutdown_argv"] and "/var/lib/openhab/.ssh/id_ed25519_power" in by["lab-pc"]["shutdown_argv"]


def test_exec_command_line_quotes_and_percent():
    cmd = exec_command_line(["ssh", "u@h", "echo 100% done"])
    assert cmd == "ssh u@h 'echo 100%% done'"      # Formatter turns %% back into %


@pytest.mark.parametrize("override,msg", [
    ({"mac": "zz:zz"}, "MAC"),
    ({"address": "1.2.3.4; rm -rf /"}, "address"),
    ({"ssh_user": "a b"}, "ssh_user"),
    ({"poweroff": "a\nb"}, "einzeilig"),
    ({"foo": 1}, "unbekannte Felder"),
])
def test_invalid_host(tmp_path, override, msg):
    ov = write(tmp_path, "o.yaml", {"hosts": {"lab-pc": override}})
    with pytest.raises(ModelError, match=msg):
        load(FIX / "hosts.yaml", ov)


def test_duplicate_ids(tmp_path):
    ov = write(tmp_path, "o.yaml", {"hosts": {"win-pc": {"id": "lab_pc"}}})
    with pytest.raises(ModelError, match="doppelte id"):
        load(FIX / "hosts.yaml", ov)


def test_host_only_in_overrides(tmp_path):
    ov = write(tmp_path, "o.yaml", {"hosts": {"manual-pc": {"address": "10.0.0.9", "mac": "aa:bb:cc:dd:ee:ff"}}})
    m = load(FIX / "hosts.yaml", ov)
    manual = next(h for h in m["hosts"] if h["name"] == "manual-pc")
    assert manual["can_wake"] and not manual["can_shutdown"]


# ---------- render + validate ----------
@pytest.mark.parametrize("lang", LANGS)
def test_render_validates(model, tmp_path, lang):
    render(model, lang, tmp_path)
    assert validate_dir(tmp_path) == []
    things = (tmp_path / "things/host_power.things").read_text()
    items = (tmp_path / "items/host_power.items").read_text()
    rule = (tmp_path / RULE[lang]).read_text()
    assert 'macAddress="aa:bb:cc:00:00:20"' in things and "old_box" not in things
    assert "exec:command:pwr_printer_reboot" not in things            # no SSH account -> no reboot thing
    assert 'autoupdate="false"' in items and "Pwr_win_pc_Reboot" in items
    assert "Pwr_printer_Power" in rule and "Pwr_old_box_Power" not in rule
    assert '"Büro-PC \\"Michael\\""' in rule


def test_only_one_rule_file(model, tmp_path):
    for lang in LANGS:
        render(model, lang, tmp_path)
    assert [rel for rel in RULE.values() if (tmp_path / rel).exists()] == [RULE["python"]]


def test_external_ping_things(tmp_path):
    ov = write(tmp_path, "o.yaml", {"settings": {"ping_things": "external"}})
    m = load(FIX / "hosts.yaml", ov)
    render(m, "js", tmp_path / "out")
    things = (tmp_path / "out/things/host_power.things").read_text()
    assert "Thing network:pingdevice" not in things
    assert 'channel="network:pingdevice:lab_pc:online"' in (tmp_path / "out/items/host_power.items").read_text()
    assert validate_dir(tmp_path / "out", external_ping=True) == []


def test_no_hosts(tmp_path):
    h = write(tmp_path, "h.yaml", {"hosts": []})
    for lang in LANGS:
        render(load(h), lang, tmp_path / lang)
        assert validate_dir(tmp_path / lang) == []


# ---------- validator negative cases ----------
def test_validator_catches_mistakes():
    things = ('Thing exec:command:x "X" [ command="ssh a@b reboot", interval=60 ]\n'
              'Thing network:pingdevice:p "P" [ macAddress="aa:bb:cc:dd:ee:ff" ]\n')
    errs, uids = check_things(things, "# empty\n")
    joined = "\n".join(errs)
    assert "nicht in exec.whitelist" in joined and "interval=0" in joined and "hostname fehlt" in joined
    items = ('Group:Switch gAll "Alle"\n'
             'Switch A_Power "A" (gAll) { channel="network:pingdevice:p:online" }\n'
             'Switch B_Power "B" (gAll) { channel="network:pingdevice:p:online" }\n'
             'String Bad (gMissing) { channel="network:pingdevice:p:online" }\n')
    errs, _ = check_items(items.replace("Group:Switch", "Group"), uids, False)
    joined = "\n".join(errs)
    assert "würde ON/OFF" in joined and "gMissing ist nicht definiert" in joined and "erwartet Switch" in joined


# ---------- rules executed against mocked openHAB APIs ----------
EVENTS = [{"item": "Pwr_lab_pc_Power", "command": "ON"}, {"item": "Pwr_lab_pc_Power", "command": "OFF"},
          {"item": "Pwr_win_pc_Power", "command": "ON"}, {"item": "Pwr_printer_Power", "command": "OFF"},
          {"item": "Unrelated", "command": "OFF"}]


def run_harness(lang, rule_file, tmp_path):
    ev = tmp_path / "events.json"
    ev.write_text(json.dumps(EVENTS))
    if lang == "js":
        node = shutil.which("node")
        if not node:
            pytest.skip("node nicht installiert")
        cmd = [node, str(HARNESS / "js_harness.js"), str(rule_file), str(ev)]
    else:
        cmd = [sys.executable, str(HARNESS / "py_harness.py"), str(rule_file), str(ev)]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


@pytest.mark.parametrize("lang", ["js", "python"])
def test_rule_behaviour(model, tmp_path, lang):
    render(model, lang, tmp_path)
    t = run_harness(lang, tmp_path / RULE[lang], tmp_path)
    assert len(t["rules"]) == 1 and t["rules"][0]["id"] == "host_power_command"
    assert sorted(x["item"] for x in t["rules"][0]["triggers"]) == \
        ["Pwr_lab_pc_Power", "Pwr_nas_lab_Power", "Pwr_printer_Power", "Pwr_win_pc_Power"]
    wol = [c["wol"] for c in t["calls"] if "wol" in c]
    execs = [c["exec"] for c in t["calls"] if "exec" in c]
    assert wol == ["network:pingdevice:pwr_lab_pc"]                       # win-pc has no MAC
    assert execs == [next(h for h in model["hosts"] if h["name"] == "lab-pc")["shutdown_argv"]]
    warns = [m for lvl, m in t["logs"] if lvl == "warn"]
    assert any("keine MAC" in w for w in warns) and any("kein SSH" in w for w in warns)


def test_dsl_table_matches_model(model, tmp_path):
    render(model, "dsl", tmp_path)
    rule = (tmp_path / RULE["dsl"]).read_text()
    assert '"Pwr_lab_pc_Power" -> newArrayList("Büro-PC \\"Michael\\"", "network:pingdevice:pwr_lab_pc", ' \
           '"/var/lib/openhab/.ssh/id_ed25519_power", "22", "ohpower@192.168.1.20", ' \
           '"sudo -n /usr/bin/systemctl poweroff")' in rule
    assert '"Pwr_printer_Power" -> newArrayList("printer", "network:pingdevice:pwr_printer", ' \
           '"/var/lib/openhab/.ssh/id_ed25519_power", "22", "", "")' in rule
    assert '"Pwr_win_pc_Power" -> newArrayList("win-pc", "", ' in rule
    assert "Item Pwr_win_pc_Power received command\nthen" in rule      # last trigger without "or"


def test_cli(tmp_path):
    root = Path(__file__).parent.parent
    out = tmp_path / "out"
    base = [sys.executable, str(root / "generate.py"), "-i", str(FIX / "hosts.yaml"),
            "--overrides", str(tmp_path / "none.yaml"), "-o", str(out)]
    r = subprocess.run([*base, "render", "--lang", "js"], capture_output=True, text=True)
    assert r.returncode == 0 and "Validierung OK" in r.stdout
    assert subprocess.run([*base, "validate"], capture_output=True).returncode == 0
    listing = subprocess.run([*base, "list"], capture_output=True, text=True).stdout
    assert "win_pc" in listing and "WoL laut ethtool aus" in listing          # old-box: supported, disabled
    bad = subprocess.run([sys.executable, str(root / "generate.py"), "-i", str(tmp_path / "missing.yaml"),
                          "render", "--lang", "js"], capture_output=True, text=True)
    assert bad.returncode == 2 and "fehlt" in bad.stderr
