"""Paths and config loading. Private data lives in me/ (git-ignored while the repo is public)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ME = ROOT / "me"
EXAMPLE = ROOT / "me.example"
PORTALS = ROOT / "config" / "portals.yml"
STATE = ME / "state" / "jobs.json"
TRACKER = ME / "tracker.csv"
RUNS = ME / "runs"
PACKETS = ME / "packets"


def me_file(name: str) -> Path:
    """Private file if present, otherwise the public template."""
    p = ME / name
    return p if p.exists() else EXAMPLE / name


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def prefs() -> dict:
    return load_yaml(me_file("preferences.yml"))


def portals() -> list[dict]:
    out = []
    for e in load_yaml(PORTALS).get("employers", []):
        e = dict(e)
        e["slug"] = re.sub(r"[^a-z0-9]+", "-", e["name"].lower()).strip("-")
        out.append(e)
    return out
