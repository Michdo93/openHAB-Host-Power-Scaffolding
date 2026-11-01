"""Render the model into openHAB files with Jinja2."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined

BUILTIN_TEMPLATES = Path(__file__).parent / "templates"

COMMON = {
    "host_power.things.j2": "things/host_power.things",
    "host_power.items.j2": "items/host_power.items",
    "exec.whitelist.j2": "misc/exec.whitelist",
}
RULES = {
    "dsl": ("host_power.rules.j2", "rules/host_power.rules"),
    "js": ("host_power.js.j2", "automation/js/host_power.js"),
    "python": ("host_power.py.j2", "automation/python/host_power.py"),
}


def string_literal(value) -> str:
    """Double-quoted literal valid in .things/.items, Rules DSL, JavaScript and Python."""
    return json.dumps(str(value), ensure_ascii=False)


def make_env(template_dir: Path | None = None) -> Environment:
    loaders = [FileSystemLoader(str(template_dir))] if template_dir else []
    loaders.append(FileSystemLoader(str(BUILTIN_TEMPLATES)))
    env = Environment(loader=ChoiceLoader(loaders), undefined=StrictUndefined, autoescape=False,
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    env.filters["q"] = string_literal
    env.filters["qlist"] = lambda seq: ", ".join(string_literal(x) for x in seq)
    return env


def render(model: dict, lang: str, out_dir: Path, template_dir: Path | None = None) -> list[Path]:
    if lang not in RULES:
        raise ValueError(f"--lang muss einer von {', '.join(RULES)} sein")
    env = make_env(template_dir)
    ctx = {**model, "lang": lang, "generated": dt.datetime.now().isoformat(timespec="seconds")}
    jobs = {**COMMON, RULES[lang][0]: RULES[lang][1]}
    written = []
    for tpl, rel in jobs.items():
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(env.get_template(tpl).render(**ctx), encoding="utf-8")
        written.append(path)
    # remove rule files of other languages from earlier runs – only one may be deployed
    for other, (_, rel) in RULES.items():
        if other != lang and (out_dir / rel).exists():
            (out_dir / rel).unlink()
    return written
