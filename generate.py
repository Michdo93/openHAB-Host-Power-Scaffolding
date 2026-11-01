#!/usr/bin/env python3
"""openHAB Host Power Scaffolding – generator.

  generate.py render --lang js        # hosts.yaml (+ overrides.yaml) -> out/
  generate.py render --lang python
  generate.py render --lang dsl
  generate.py validate                # check out/ again
  generate.py list                    # show hosts and their features

The output mirrors $OPENHAB_CONF (things/, items/, misc/, rules/ or automation/…);
nothing is written into openHAB itself.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ohpower import __version__
from ohpower.model import ModelError, load
from ohpower.render import render
from ohpower.validate import validate_dir


def _model(a):
    if not a.hosts.exists():
        raise ModelError(f"{a.hosts} fehlt – mit ansible/export_hosts.yml erzeugen oder von Hand anlegen")
    ov = a.overrides if a.overrides.exists() else None
    return load(a.hosts, ov)


def cmd_render(a) -> int:
    model = _model(a)
    if not model["active"]:
        print("[!] keine aktivierten Hosts", file=sys.stderr)
    for p in render(model, a.lang, a.out, a.templates):
        print(f"[+] {p}")
    return _validate(a.out, model)


def _validate(out: Path, model: dict) -> int:
    errors = validate_dir(out, external_ping=model["settings"]["ping_things"] == "external")
    for e in errors:
        print(f"[!] {e}", file=sys.stderr)
    print("[+] Validierung OK" if not errors else f"[-] {len(errors)} Fehler")
    return 1 if errors else 0


def cmd_validate(a) -> int:
    return _validate(a.out, _model(a))


def cmd_list(a) -> int:
    model = _model(a)
    yes = lambda b: "ja " if b else "-  "
    print(f"{'id':<20} {'Adresse':<22} {'OS':<8} WoL Aus Neu  aktiv")
    for h in model["hosts"]:
        addr = f"{h['address']}:{h['port']}"
        print(f"{h['id']:<20} {addr:<22} {h['os']:<8} {yes(h['can_wake'])} {yes(h['can_shutdown'])} "
              f"{yes(h['can_reboot'])}  {'ja' if h['enabled'] else 'nein'}"
              f"{'  (WoL laut ethtool aus)' if h.get('wol_enabled') is False and h['can_wake'] else ''}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-i", "--hosts", type=Path, default=Path("hosts.yaml"))
    ap.add_argument("--overrides", type=Path, default=Path("overrides.yaml"))
    ap.add_argument("-o", "--out", type=Path, default=Path("out"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("render", help="Things, Items, Whitelist und Regel erzeugen")
    p.add_argument("--lang", required=True, choices=["dsl", "js", "python"],
                   help="Sprache der Regel (nur eine Variante deployen!)")
    p.add_argument("--templates", type=Path, help="eigenes Template-Verzeichnis")
    p.set_defaults(fn=cmd_render)
    sub.add_parser("validate", help="out/ prüfen").set_defaults(fn=cmd_validate)
    sub.add_parser("list", help="Hosts und Funktionen anzeigen").set_defaults(fn=cmd_list)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (ModelError, ValueError, FileNotFoundError) as e:
        print(f"[-] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
