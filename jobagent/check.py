"""Anti-fabrication check for tailored CVs and cover notes. Mechanical, no LLM.

A tailored CV is a JSON file whose bullets each cite the profile line IDs they are
built from. This check fails the CV when a bullet:
  1. cites no source, an unknown ID, or a line that is not `confirmed`;
  2. contains a number that its sources do not contain;
  3. names a tool, organisation or acronym that its sources do not contain;
  4. uses a strength word (led, expert, production...) its sources do not use;
  5. reuses wording from a `needs-proof` line.
It reduces errors. It cannot catch every rewording, so a human still reads every CV.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from . import profile as prof

TOOLS = """python r sql excel tableau power bi powerbi sas stata matlab spark pyspark hadoop airflow dbt
bigquery snowflake redshift databricks postgres postgresql mysql oracle mongodb aws azure gcp docker kubernetes git
github pytorch tensorflow keras scikit-learn sklearn xgboost lightgbm catboost pandas numpy langchain llm llms rag
javascript typescript java scala c++ go rust vba looker qlik alteryx knime jira fastapi flask django streamlit
mlflow kafka terraform linux bash openai claude gpt hugging face shap arima prophet""".split()
TOOL_PHRASES = ["power bi", "hugging face", "scikit-learn", "machine learning", "deep learning",
                "gradient boosting", "logistic regression", "random forest", "neural network",
                "credit scoring", "scorecard", "ifrs 9", "basel", "icaap", "stress test", "pd", "lgd", "ecl",
                "a/b test", "time series", "nlp", "computer vision", "reinforcement learning"]
STRENGTH = """led lead leading managed managing headed owned owning spearheaded architected designed expert
expertise advanced proficient fluent extensive deep production deployed senior independently single-handedly
significantly dramatically substantially award-winning top best first only pioneered launched founded
halved doubled tripled half twice""".split()
COMMON_CAPS = set("""I A An The And Or For In On At To Of With By From As Is Was Were Are Be Been This That
January February March April May June July August September October November December
Jan Feb Mar Apr Jun Jul Aug Sep Sept Oct Nov Dec Present Remote Summary Experience Education Skills Projects
Languages English""".split())

NUMWORDS = {"two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
            "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "fifteen": "15", "twenty": "20",
            "thirty": "30", "forty": "40", "fifty": "50", "hundred": "100", "hundreds": "100",
            "thousand": "1000", "thousands": "1000", "million": "1000000", "millions": "1000000",
            "dozen": "12", "dozens": "12", "several": "", "numerous": "", "multiple": "", "many": "",
            "double-digit": "", "triple-digit": ""}
# First word of a bullet may be any ordinary CV verb; anything else capitalised must come from the source.
OPENERS = set("""built adapted extended served defined automated analyzed analysed prototyped organized organised
prepared quantified identified applied practised practiced developed created wrote used worked supported assisted
contributed produced tested documented reviewed mapped cleaned queried modelled modeled forecast forecasted
junior analyst data python methods recipient coursework focus listed capstone vice project financial
cross-country used-car msc bsc phd also as in on for with during while using through i my a an the""".split())


def _norm(s: str) -> str:
    """Fold look-alike characters (full-width digits, non-breaking spaces) before checking."""
    return unicodedata.normalize("NFKC", s).replace("\u2019", "'").replace("\u2013", "-").replace("\u2014", "-")


_NUM = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#./&'-]*")


def _nums(s: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(s)}


def _low_words(s: str) -> set[str]:
    return {w.lower().strip(".'") for w in _WORD.findall(s)}


def _entities(s: str, strict_first: bool = True) -> set[str]:
    """Capitalised or all-caps tokens. A sentence's first word counts too unless it is an ordinary opener."""
    out = set()
    for sent in re.split(r"(?<=[.;:!?])\s+", s):
        words = _WORD.findall(sent)
        for i, w in enumerate(words):
            w = re.sub(r"'s$", "", w.strip(".'"))
            if not w or w in COMMON_CAPS:
                continue
            if len(w) > 1 and w.isupper():
                out.add(w)
            elif w[0].isupper() and (i > 0 or (strict_first and w.lower() not in OPENERS)):
                out.add(w)
    return out


def _stem_in(word: str, vocab: set[str]) -> bool:
    stem = word[:5] if len(word) > 5 else word
    return any(v.startswith(stem) for v in vocab)


def _shingles(s: str, n: int = 4) -> set[tuple]:
    w = re.findall(r"[a-z0-9]+", s.lower())
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def check_item(text: str, sources: list[str], profile: dict, where: str, banned: set[tuple]) -> list[str]:
    errs = []
    lines = profile["lines"]
    if not sources:
        return [f"{where}: no source cited"]
    src_text = ""
    for sid in sources:
        l = lines.get(sid)
        if not l:
            errs.append(f"{where}: unknown source {sid}")
            continue
        if l["status"] != "confirmed":
            errs.append(f"{where}: source {sid} is needs-proof ({l['why']})")
        src_text += " " + l["text"]
        if l["entry"]:
            e = profile["entries"][l["entry"]]
            src_text += " " + " ".join((e["org"], e["role"], e["dates"], e["location"]))
    if errs:
        return errs
    text, src_text = _norm(text), _norm(src_text)
    extra = _nums(text) - _nums(src_text)
    if extra:
        errs.append(f"{where}: number(s) not in sources {sources}: {sorted(extra)}")
    low_src, low_txt = _low_words(src_text), _low_words(text)
    words = {w for w in NUMWORDS if w in low_txt and w not in low_src and NUMWORDS[w] not in _nums(src_text)}
    if words:
        errs.append(f"{where}: quantity word(s) not in sources {sources}: {sorted(words)}")
    tl, sl = " " + text.lower() + " ", " " + src_text.lower() + " "
    tools = {t for t in TOOLS if t in low_txt and t not in low_src}
    tools |= {p for p in TOOL_PHRASES if re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", tl)
              and not re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", sl)}
    if tools:
        errs.append(f"{where}: tool/skill not in sources {sources}: {sorted(tools)}")
    src_ents_low = {e.lower() for e in _entities(src_text, False)} | low_src
    ents = {e for e in _entities(text) if e.lower() not in src_ents_low and e.lower() not in tools}
    if ents:
        errs.append(f"{where}: name/acronym not in sources {sources}: {sorted(ents)}")
    strong = {w for w in STRENGTH if w in low_txt and not _stem_in(w, low_src)}
    if strong:
        errs.append(f"{where}: stronger wording than source: {sorted(strong)}")
    hit = _shingles(text) & banned
    if hit:
        errs.append(f"{where}: reuses wording from a needs-proof line: '{' '.join(sorted(hit)[0])}'")
    return errs


def _banned(profile: dict) -> set[tuple]:
    ok, bad = set(), set()
    for l in profile["lines"].values():
        (ok if l["status"] == "confirmed" else bad).update(_shingles(l["text"]))
    # generic four-word runs ("extended the framework to") are not evidence of leakage
    stop = set("a an the to of in on for by and or with from as at is was be than that this more most its into across".split())
    return {g for g in bad - ok if sum(w not in stop for w in g) >= 3}


def iter_items(cv: dict):
    """Yield (where, text, sources) for every free-text item in a cv.json."""
    if cv.get("summary"):
        yield "summary", cv["summary"]["text"], cv["summary"].get("sources", [])
    for sec in cv.get("experience", []):
        for i, b in enumerate(sec.get("bullets", []), 1):
            yield f"experience {sec['entry']} bullet {i}", b["text"], b.get("sources", [])
    for key in ("projects", "skills", "extras"):
        for i, b in enumerate(cv.get(key, []), 1):
            yield f"{key} {i}", b["text"], b.get("sources", [])


def check_cv(cv: dict, profile: dict) -> list[str]:
    errs, banned = [], _banned(profile)
    if not isinstance(cv, dict) or not any(cv.get(k) for k in ("experience", "projects", "summary")):
        return ["cv.json has no content"]
    for where, text, _ in iter_items(cv):
        if len(text) > 420:
            errs.append(f"{where}: over 420 characters; split it so each claim can be traced")
    for sec in cv.get("experience", []) + [{"entry": e} for e in cv.get("education", [])]:
        if sec["entry"] not in profile["entries"]:
            errs.append(f"unknown entry {sec['entry']}")
    for sec in cv.get("experience", []):
        e = profile["entries"].get(sec["entry"])
        for i, b in enumerate(sec.get("bullets", []), 1):
            for sid in b.get("sources", []):
                l = profile["lines"].get(sid)
                if e and l and l["entry"] not in (sec["entry"], None):
                    errs.append(f"experience {sec['entry']} bullet {i}: source {sid} belongs to {l['entry']}")
    for where, text, sources in iter_items(cv):
        errs += check_item(text, sources, profile, where, banned)
    return errs


_YEARS_CLAIM = re.compile(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)


def check_text(text: str, profile: dict, posting: str = "", own: str = "") -> list[str]:
    """Check for cover notes and answers. `posting` may lend names (employer, team, city) but
    never facts about the candidate: numbers, years and tools must come from the confirmed
    profile or from `own` (the answer bank)."""
    text = _norm(text)
    mine = _norm(prof.confirmed_text(profile) + "\n" + own)
    errs = []
    extra = _nums(text) - _nums(mine)
    extra -= {n for n in extra if re.search(r"(?<![\d.])" + re.escape(n) + r"(?![\d.])", posting)
              and not re.search(re.escape(n) + r"\s*\+?\s*(years?|yrs?)", text, re.I)}
    if extra:
        errs.append(f"number(s) not in profile or answer bank: {sorted(extra)}")
    yrs = {m.group(1) for m in _YEARS_CLAIM.finditer(text)} - {m.group(1) for m in _YEARS_CLAIM.finditer(mine)}
    if yrs:
        errs.append(f"years claim not in profile or answer bank: {sorted(yrs)}")
    low_txt, low_ok = _low_words(text), _low_words(mine)
    tools = {t for t in TOOLS if t in low_txt and t not in low_ok}
    if tools:
        errs.append(f"tool/skill claimed but not in confirmed profile: {sorted(tools)}")
    known = {e.lower() for e in _entities(mine + "\n" + posting, False)} | low_ok | _low_words(posting)
    ents = {e for e in _entities(text, False) if e.lower() not in known and e.lower() not in tools}
    if ents:
        errs.append(f"name/acronym not in profile, answer bank or posting: {sorted(ents)}")
    strong = {w for w in STRENGTH if w in low_txt and not _stem_in(w, low_ok)}
    strong -= {"first", "only", "best", "top", "deep", "half", "lead", "leading", "senior", "advanced", "production"} & _low_words(posting)
    if strong:
        errs.append(f"strength word not supported by the profile: {sorted(strong)}")
    hit = _shingles(text) & _banned(profile)
    if hit:
        errs.append(f"reuses wording from a needs-proof line: '{' '.join(sorted(hit)[0])}'")
    return errs


def _contact_text(profile_path: Path) -> str:
    c = profile_path.parent / "contact.yml"
    return c.read_text(encoding="utf-8") if c.exists() else ""


def run(packet_dir: Path, profile_path: Path) -> tuple[bool, str]:
    profile = prof.load(profile_path)
    report, ok = [], True
    cvp = packet_dir / "cv.json"
    if cvp.exists():
        errs = check_cv(json.loads(cvp.read_text(encoding="utf-8")), profile)
        ok &= not errs
        report.append(f"cv.json: {'PASS' if not errs else 'FAIL'}")
        report += [f"  - {e}" for e in errs]
    else:
        ok = False
        report.append("cv.json: MISSING")
    posting = (packet_dir / "posting.txt").read_text(encoding="utf-8") if (packet_dir / "posting.txt").exists() else ""
    bank = profile_path.parent / "answer_bank.md"
    bank_text = bank.read_text(encoding="utf-8") if bank.exists() else ""
    for name in ("cover_note.md", "answers.md"):
        p = packet_dir / name
        if p.exists():
            rows = [l for l in p.read_text(encoding="utf-8").splitlines() if not l.startswith("#")]
            if name == "answers.md":  # check the answers, not the form's question labels
                rows = [l.split(": ", 1)[1] if ": " in l else "" for l in rows]
            body = "\n".join(rows)
            errs = check_text(body, profile, posting, bank_text + "\n" + _contact_text(profile_path))
            ok &= not errs
            report.append(f"{name}: {'PASS' if not errs else 'FAIL'}")
            report += [f"  - {e}" for e in errs]
    text = "\n".join(report)
    (packet_dir / "check_report.txt").write_text(text + "\n", encoding="utf-8")
    return ok, text
