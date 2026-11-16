"""Parse master_profile.md: the single source of truth for every CV fact.

Format:
  ## Section
  ### X1 | Organisation | Role | Dates | Location      <- an entry header (locked, used verbatim)
  - [E1] (confirmed) Fact text.                         <- usable in CVs
  - [E2] (needs-proof: why) Fact text.                  <- never usable until confirmed
"""
from __future__ import annotations

import re
from pathlib import Path

_LINE = re.compile(r"^- \[([A-Z]{1,3}\d+)\] \((confirmed|needs-proof)(?::\s*([^)]*))?\)\s+(.*\S)\s*$")
_ENTRY = re.compile(r"^### ([A-Z]{1,3}\d+)\s*\|\s*(.*)$")


def load(path: Path) -> dict:
    lines, entries, section, entry = {}, {}, "", None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("## "):
            section, entry = raw[3:].strip(), None
            continue
        m = _ENTRY.match(raw)
        if m:
            parts = [p.strip() for p in m.group(2).split("|")]
            parts += [""] * (4 - len(parts))
            entry = m.group(1)
            entries[entry] = {"id": entry, "section": section, "org": parts[0], "role": parts[1],
                              "dates": parts[2], "location": parts[3], "lines": []}
            continue
        m = _LINE.match(raw)
        if m:
            lid, status, why, text = m.groups()
            if lid in lines:
                raise ValueError(f"duplicate profile line id {lid}")
            lines[lid] = {"id": lid, "status": status, "why": why or "", "text": text,
                          "section": section, "entry": entry}
            if entry:
                entries[entry]["lines"].append(lid)
    return {"lines": lines, "entries": entries}


def confirmed_text(profile: dict) -> str:
    """Everything a CV is allowed to draw on, as one string."""
    parts = [l["text"] for l in profile["lines"].values() if l["status"] == "confirmed"]
    for e in profile["entries"].values():
        parts.append(" ".join((e["org"], e["role"], e["dates"], e["location"])))
    return "\n".join(parts)


def summary(profile: dict) -> str:
    conf = sum(1 for l in profile["lines"].values() if l["status"] == "confirmed")
    need = [l for l in profile["lines"].values() if l["status"] != "confirmed"]
    out = [f"{conf} confirmed lines, {len(need)} needing proof, {len(profile['entries'])} entries."]
    out += [f"  needs-proof {l['id']}: {l['why'] or 'unspecified'}" for l in need]
    return "\n".join(out)
