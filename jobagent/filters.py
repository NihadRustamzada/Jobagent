"""Deterministic hard filters and a cheap pre-score. No LLM, no cost.

The hard filters drop postings that cannot work (too senior, wrong place, a
required language, citizens only, excluded employer, stale). The pre-score only
ranks what is left so the expensive judgement step reads the best candidates first.
"""
from __future__ import annotations

import re
from datetime import date

_YEARS = re.compile(
    r"(?<![\d.$€£])(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*)?(?:years?|yrs?)\b", re.I)
_SOFT = re.compile(r"\b(plus|advantage|bonus|nice to have|preferred|desirable|beneficial|asset|ideally|a second language)\b", re.I)
_NEED = re.compile(r"\b(fluen\w+|native|proficien\w+|required|must|mandatory|essential|speaker|speaking|business[- ]level|C1|C2)\b", re.I)


def _has(text: str, term: str) -> bool:
    """Word-boundary match so 'UK' does not hit 'Ukraine' and 'risk' does not hit 'asterisk'."""
    return re.search(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])", text, re.I) is not None


def min_years_required(desc: str) -> int:
    """Largest lower bound among 'N years' phrases that sit near the word experience."""
    best = 0
    for m in _YEARS.finditer(desc):
        n = int(m.group(1))
        if n > 20:
            continue
        ctx = desc[max(0, m.start() - 90): m.end() + 90].lower()
        after = desc[m.end(): m.end() + 12].lower().lstrip(" '’")
        if ("experience" in ctx or "background" in ctx or "track record" in ctx
                or after.startswith(("of ", "in ", "on ", "as ", "working", "with ", "at "))):
            best = max(best, n)
    return best


def language_required(desc: str, languages: list[str]) -> str:
    for sent in re.split(r"(?<=[.!?;])\s+|\n", desc):
        for lang in languages:
            if _has(sent, lang) and _NEED.search(sent) and not _SOFT.search(sent):
                return lang
    return ""


def location_ok(job: dict, prefs: dict) -> bool:
    loc = job.get("location") or ""
    for target in prefs["locations"]:
        if any(_has(loc, m) for m in target["match"]):
            return True
    if job.get("remote") or _has(loc, "remote"):
        rest = re.sub(r"remote|[-–,;()/|]", " ", loc, flags=re.I).strip()
        if not rest:  # plain "Remote"
            return True
        return any(_has(loc, m) for m in prefs.get("remote_regions", []))
    return False


def title_family(title: str, prefs: dict) -> str:
    for fam, cfg in prefs["role_families"].items():
        if any(_has(title, k) for k in cfg["title_keywords"]):
            return fam
    return ""


def hard_filter(job: dict, prefs: dict, today: date) -> list[str]:
    """Reasons to drop. Empty list means the job passes."""
    reasons = []
    title, desc = job.get("title", ""), job.get("description", "")
    f = prefs["filters"]
    if any(_has(job["employer"], x) for x in prefs.get("excluded_employers", [])):
        reasons.append("excluded employer")
    if any(_has(title, x) for x in f["title_exclude"]):
        reasons.append("title too senior or off-target")
    if not title_family(title, prefs):
        reasons.append("title outside target role families")
    if not location_ok(job, prefs):
        reasons.append(f"location not targeted: {job.get('location', '')[:60]}")
    if desc:
        yrs = min_years_required(desc)
        if yrs > f["max_years_required"]:
            reasons.append(f"asks for {yrs}+ years")
        lang = language_required(title + ". " + desc, f["languages_lacking"])
        if lang:
            reasons.append(f"requires {lang}")
        for pat in f["citizens_only_patterns"]:
            if re.search(pat, desc, re.I):
                reasons.append("citizens-only or nationalisation-reserved")
                break
        for pat in f.get("scam_patterns", []):
            if re.search(pat, desc, re.I):
                reasons.append("legitimacy: asks for payment or personal banking details")
                break
    if job.get("posted_at"):
        try:
            age = (today - date.fromisoformat(job["posted_at"])).days
            if age > f["max_age_days"]:
                reasons.append(f"posted {age} days ago")
            elif age > f.get("stale_after_days", 45):
                job["stale_days"] = age
        except ValueError:
            pass
    return reasons


def prescore(job: dict, prefs: dict) -> int:
    """0 to 100. A ranking aid only; the 1 to 5 fit score comes from the rubric step."""
    title, desc = job.get("title", ""), job.get("description", "")
    s = 0
    fam = title_family(title, prefs)
    if fam:
        cfg = prefs["role_families"][fam]
        s += 25 * cfg.get("priority", 1.0)
        hits = sum(1 for k in cfg.get("desc_keywords", []) if _has(desc, k))
        s += min(hits, 10) * 3.5
    sig = prefs["filters"].get("signals", {})
    s += 8 * sum(1 for k in sig.get("junior", []) if _has(title, k))
    s -= 12 * sum(1 for k in sig.get("senior", []) if _has(title, k))
    s += min(3, sum(1 for k in sig.get("positive", []) if _has(desc, k))) * 4
    s -= min(3, sum(1 for k in sig.get("negative", []) if _has(desc, k))) * 5
    if job.get("stale_days"):
        s -= 8
    yrs = min_years_required(desc)
    if 1 <= yrs <= 3:
        s += 10
    elif yrs >= 4:
        s -= 6
    return max(0, min(100, round(s)))
