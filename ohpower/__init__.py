"""openHAB Host Power Scaffolding.

hosts.yaml (Ansible export or hand-written) + overrides.yaml
  -> Jinja2 -> Things (Network + Exec Binding), Items, exec.whitelist and
     one rule file in Rules DSL, JavaScript (GraalJS) or Python (GraalPy).
"""

__version__ = "1.0.0"
