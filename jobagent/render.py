"""Render a checked cv.json to a plain, single-column, ATS-readable .docx and a .md preview.

Structure is locked: employer, role, dates and education lines come verbatim from the
master profile by ID. Only bullet wording is tailored, and that wording is checked.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config, profile as prof


def _contact() -> dict:
    return config.load_yaml(config.me_file("contact.yml"))


def build_blocks(cv: dict, profile: dict, contact: dict) -> list[tuple]:
    """Flat list of (kind, text[, right]) used by both renderers."""
    L, E = profile["lines"], profile["entries"]
    out = [("name", contact.get("name", ""))]
    bits = [contact.get(k) for k in ("city", "phone", "email")] + list((contact.get("links") or {}).values())
    out.append(("contact", "  |  ".join(b for b in bits if b)))
    if cv.get("summary"):
        out += [("h", "SUMMARY"), ("p", cv["summary"]["text"])]
    if cv.get("experience"):
        out.append(("h", "EXPERIENCE"))
        for sec in cv["experience"]:
            e = E[sec["entry"]]
            out.append(("entry", f"{e['org']} - {e['role']}", e["dates"]))
            if e["location"]:
                out.append(("sub", e["location"]))
            out += [("b", b["text"]) for b in sec.get("bullets", [])]
    if cv.get("projects"):
        out.append(("h", "PROJECTS"))
        out += [("b", b["text"]) for b in cv["projects"]]
    if cv.get("education"):
        out.append(("h", "EDUCATION"))
        for eid in cv["education"]:
            e = E[eid]
            out.append(("entry", f"{e['org']} - {e['role']}", e["dates"]))
            keep = cv.get("education_lines", {}).get(eid)
            for lid in e["lines"]:
                if L[lid]["status"] == "confirmed" and (keep is None or lid in keep):
                    out.append(("b", L[lid]["text"]))
    if cv.get("skills"):
        out.append(("h", "SKILLS"))
        out += [("b", b["text"]) for b in cv["skills"]]
    if cv.get("extras"):
        out.append(("h", "ADDITIONAL"))
        out += [("b", b["text"]) for b in cv["extras"]]
    return out


def to_markdown(blocks) -> str:
    md = []
    for b in blocks:
        k, t = b[0], b[1]
        if k == "name":
            md.append(f"# {t}")
        elif k == "contact":
            md.append(t + "\n")
        elif k == "h":
            md.append(f"\n## {t}")
        elif k == "entry":
            md.append(f"\n**{t}** | {b[2]}")
        elif k == "sub":
            md.append(f"*{t}*")
        elif k == "b":
            md.append(f"- {t}")
        else:
            md.append(t)
    return "\n".join(md) + "\n"


def to_docx(blocks, path: Path) -> None:
    from docx import Document
    from docx.enum.text import WD_TAB_ALIGNMENT
    from docx.shared import Cm, Pt

    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(1.8)
    sec.top_margin = sec.bottom_margin = Cm(1.5)
    width = sec.page_width - sec.left_margin - sec.right_margin
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Calibri", Pt(10.5)
    st.paragraph_format.space_after = Pt(2)

    for b in blocks:
        k, t = b[0], b[1]
        if k == "name":
            p = doc.add_paragraph()
            r = p.add_run(t.upper())
            r.bold, r.font.size = True, Pt(16)
        elif k == "h":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(8)
            r = p.add_run(t)
            r.bold, r.font.size = True, Pt(11)
        elif k == "entry":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
            p.add_run(t).bold = True
            p.add_run("\t" + b[2])
        elif k == "sub":
            doc.add_paragraph().add_run(t).italic = True
        elif k == "b":
            doc.add_paragraph(t, style="List Bullet")
        else:
            doc.add_paragraph(t)
    doc.save(str(path))


def render(packet_dir: Path, profile_path: Path) -> list[Path]:
    cv = json.loads((packet_dir / "cv.json").read_text(encoding="utf-8"))
    blocks = build_blocks(cv, prof.load(profile_path), _contact())
    md = packet_dir / "cv_preview.md"
    md.write_text(to_markdown(blocks), encoding="utf-8")
    out = [md]
    try:
        to_docx(blocks, packet_dir / "cv.docx")
        out.append(packet_dir / "cv.docx")
    except ImportError:
        print("python-docx not installed: wrote the markdown preview only (pip install python-docx)")
    return out
