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


_WORDNUM = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "twelve": 12, "fifteen": 15}
_YEARS_W = re.compile(r"\b(" + "|".join(_WORDNUM) + r")\s*\+?\s*(?:or more\s+)?(?:years?|yrs?)\b", re.I)
_NOT_REQ = re.compile(r"(last|next|past|over the|coming|previous|within|every|for over|in)\s*$", re.I)


def min_years_required(desc: str) -> int:
    """Largest lower bound among 'N years' phrases that read as an experience requirement.

    Counts 'N+ years', or 'N years' with the word experience close by. Ignores company
    history ('in the last 10 years') and ages ('16-17 year olds').
    """
    best = 0
    hits = [(m, int(m.group(1)), "+" in m.group(0) or "plus" in m.group(0).lower()) for m in _YEARS.finditer(desc)]
    hits += [(m, _WORDNUM[m.group(1).lower()], "+" in m.group(0)) for m in _YEARS_W.finditer(desc)]
    for m, n, plus in hits:
        if n > 20:
            continue
        before, after = desc[max(0, m.start() - 45): m.start()], desc[m.end(): m.end() + 70].lower()
        if _NOT_REQ.search(before) or after.lstrip(" -").startswith("old"):
            continue
        near = "experience" in after or "experience" in before.lower() or "track record" in after
        if plus or near or after.lstrip(" '’").startswith(("of ", "working", "as a", "in a ")):
            best = max(best, n)
    return best


def language_required(desc: str, languages: list[str]) -> str:
    for sent in re.split(r"(?<=[.!?;])\s+|\n", desc):
        for lang in languages:
            if _has(sent, lang) and _NEED.search(sent) and not _SOFT.search(sent):
                return lang
    return ""


_OFFERS = re.compile(r"\b(can|do|will|able to|happy to)\s+(offer |provide )?(visa )?sponsor|sponsor visas? (to|for|in)", re.I)
_INJECT = re.compile(r"ignore (all |any )?(previous|prior|above) (instructions|prompts)|disregard (the|all|any) (previous|above)"
                     r"|you are an? (ai|llm|language model)|as an ai\b|system prompt|rate this (candidate|applicant)"
                     r"|(ai|llm|automated) (agents?|assistants?|tools?|systems?)[^.\n]{0,60}(must|should|include|mention|write)", re.I)


def sponsorship_block(desc: str, patterns: list[str]) -> str:
    """'drop' if the posting rules out sponsorship outright; a note if it is conditional; '' otherwise."""
    for pat in patterns:
        m = re.search(pat, desc, re.I)
        if not m:
            continue
        a = max(desc.rfind(".", 0, m.start()), desc.rfind("\n", 0, m.start())) + 1
        b = min([x for x in (desc.find(".", m.end()), desc.find("\n", m.end())) if x != -1] or [len(desc)])
        sent = desc[a:b].strip()
        if _OFFERS.search(re.sub(pat, " ", sent, flags=re.I)):
            return "conditional sponsorship: " + sent[:160]
        return "drop"
    return ""


def location_ok(job: dict, prefs: dict) -> bool:
    loc = job.get("location") or ""
    for target in prefs["locations"]:
        if any(_has(loc, m) for m in target["match"]) and not any(_has(loc, x) for x in target.get("exclude", [])):
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
    if not desc and any(re.search(pat, title, re.I) for pat in f["citizens_only_patterns"]):
        reasons.append("citizens-only or nationalisation-reserved")
    if desc:
        yrs = min_years_required(desc)
        if yrs > f["max_years_required"]:
            reasons.append(f"asks for {yrs}+ years")
        lang = language_required(title + ". " + desc, f["languages_lacking"])
        if lang:
            reasons.append(f"requires {lang}")
        for pat in f["citizens_only_patterns"]:
            if re.search(pat, title + "\n" + desc, re.I):
                reasons.append("citizens-only or nationalisation-reserved")
                break
        note = sponsorship_block(desc, f.get("no_sponsorship_patterns", []))
        if note == "drop":
            reasons.append("no visa sponsorship")
        elif note:
            job["visa_flag"] = note
        if _INJECT.search(desc):
            job["injection_flag"] = True
        for pat in f.get("scam_patterns", []):
            if re.search(pat, desc, re.I):
                reasons.append("legitimacy: asks for payment or personal banking details")
                break
    if job.get("posted_at"):
        try:
            age = (today - date.fromisoformat(job["posted_at"])).days
            if f.get("max_age_days") and age > f["max_age_days"]:
                reasons.append(f"posted {age} days ago")
            elif age > f.get("stale_after_days", 60):
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
