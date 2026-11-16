"""Weekly pipeline: discover -> filter -> (judgement step writes scores.json) -> apply scores -> digest."""
from __future__ import annotations

import concurrent.futures as cf
import gzip
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path

from . import ats, config, filters, profile as prof, tracker

DESC_LIMIT = 6000
DIMS = ("skill", "seniority", "pay", "visa", "build")


def _load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")


def fingerprint(job: dict) -> str:
    t = re.sub(r"\(.*?\)|[^a-z0-9 ]", " ", job["title"].lower())
    return job["employer_slug"] + "|" + " ".join(t.split())


def run_dir(today: date) -> Path:
    return config.RUNS / today.isoformat()


# ------------------------------------------------------------------ discover

def discover(today: date, rescore_all: bool = False, only: str | None = None) -> dict:
    prefs = config.prefs()
    emps = [e for e in config.portals() if e["ats"] != "search" and (not only or only.lower() in e["name"].lower())]
    searches = prefs.get("workday_searches") or [""]
    health, raw = {}, []

    cache_dir = config.STATE.parent / "cache"

    def pull(emp):
        """Live fetch; on failure fall back to the last good copy if it is under 14 days old."""
        cache = cache_dir / f"{emp['slug']}.json.gz"
        try:
            jobs = ats.fetch(emp, searches)
            if jobs:
                cache.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(cache, "wt", encoding="utf-8") as f:
                    json.dump({"date": today.isoformat(), "jobs": jobs}, f)
            return emp, jobs, None, ""
        except Exception as e:  # one broken board must not stop the run
            old = None
            if cache.exists():
                try:
                    with gzip.open(cache, "rt", encoding="utf-8") as f:
                        old = json.load(f)
                except (OSError, ValueError):
                    old = None
            if old and (today - date.fromisoformat(old["date"])).days <= 14:
                return emp, old["jobs"], None, old["date"]
            return emp, [], str(e)[:100], ""

    with cf.ThreadPoolExecutor(8) as ex:
        for emp, jobs, err, cached in ex.map(pull, emps):
            health[emp["name"]] = {"ats": emp["ats"], "postings": len(jobs), "error": err, "cached": cached}
            raw += jobs

    state = _load(config.STATE, {})
    tracked = tracker.load()
    drops, passed = Counter(), []
    for job in raw:
        # cheap checks first, so the per-posting detail call is made only for plausible jobs
        pre = filters.hard_filter({**job, "description": ""}, prefs, today)
        if not pre and not job["description"]:
            try:
                ats.enrich(job)
            except Exception:  # an odd detail payload must not stop the run
                pass
        reasons = pre or filters.hard_filter(job, prefs, today)
        s = state.setdefault(job["id"], {"first_seen": today.isoformat(), "fp": fingerprint(job),
                                         "title": job["title"], "employer": job["employer"]})
        s["last_seen"] = today.isoformat()
        s["dropped"] = reasons
        if reasons:
            for r in reasons:
                drops[re.sub(r"[:\d].*", "", r).strip()] += 1
            continue
        job["prescore"] = filters.prescore(job, prefs)
        job["family"] = filters.title_family(job["title"], prefs)
        job["years_asked"] = filters.min_years_required(job["description"])
        passed.append(job)

    # merge the same role posted in several locations; flag reposts of roles seen under an older id
    merged = {}
    for job in sorted(passed, key=lambda j: -j["prescore"]):
        fp = fingerprint(job)
        if fp in merged:
            merged[fp].setdefault("also_locations", []).append(job["location"])
            continue
        older = [v["first_seen"] for k, v in state.items()
                 if v.get("fp") == fp and k != job["id"] and v["first_seen"] < today.isoformat()]
        if older:
            job["repost_of_role_first_seen"] = min(older)
        merged[fp] = job

    cands = []
    for job in merged.values():
        row = tracked.get(job["id"])
        if row and row.get("score") and not rescore_all:
            continue  # already judged in an earlier run
        job.pop("_detail", None)
        job["description"] = job["description"][:DESC_LIMIT]
        cands.append(job)
    cands = cands[: prefs.get("max_candidates_per_run", 30)]

    rd = run_dir(today)
    # a second run on the same day must not lose candidates that were already judged today
    have = {j["id"] for j in cands}
    judged = {x.get("id") for x in _load(rd / "scores.json", []) if isinstance(x, dict)}
    cands += [j for j in _load(rd / "candidates.json", []) if j["id"] not in have and j["id"] in judged]

    # tracked roles whose posting disappeared from a board that answered: probably closed
    live, ok_emps = {j["id"] for j in raw}, {n for n, h in health.items() if not h["error"]}
    gone = [r["id"] for r in tracked.values() if r["status"] in ("shortlisted", "packet_ready")
            and r["employer"] in ok_emps and r["id"] not in live and r["ats"] != "search"]
    _dump(config.STATE, state)
    _dump(rd / "candidates.json", cands)
    report = {"date": today.isoformat(), "postings_read": len(raw), "passed_filters": len(merged),
              "new_candidates": len(have), "gone": gone, "drop_reasons": dict(drops.most_common()), "health": health}
    _dump(rd / "discover_report.json", report)
    return report


# ------------------------------------------------------------------ scoring

def _skill_cap(reqs: list[dict]) -> float:
    """The skill dimension may not exceed what the requirement evidence supports."""
    must = [r for r in reqs if r.get("must", True)]
    if not must:
        return 5.0
    credit = sum({"met": 1.0, "partial": 0.5}.get(r["status"], 0.0) for r in must)
    return round((1 + 4 * credit / len(must)) * 2) / 2


def apply_scores(today: date) -> list[dict]:
    """Validate the judgement step's scores.json, compute the weighted score in code, update the tracker."""
    prefs, rd = config.prefs(), run_dir(today)
    profile = prof.load(config.me_file("master_profile.md"))
    cands = {j["id"]: j for j in _load(rd / "candidates.json", [])}
    scores = _load(rd / "scores.json", None)
    if scores is None:
        raise SystemExit(f"{rd / 'scores.json'} not found. The judgement step writes it (see the runbook).")
    w, sc = prefs["scoring"]["weights"], prefs["scoring"]
    errors, results, rows = [], [], tracker.load()
    if not isinstance(scores, list):
        raise SystemExit("scores.json must be a list of objects")
    seen_ids = set()
    for s in scores:
        if not isinstance(s, dict):
            errors.append("every entry must be an object")
            continue
        if s.get("id") in seen_ids:
            errors.append(f"{s.get('id')}: scored twice")
        seen_ids.add(s.get("id"))
        if not isinstance(s.get("requirements"), list) or not s["requirements"]:
            errors.append(f"{s.get('id')}: needs a non-empty requirements list")
            s["requirements"] = []
        job = cands.get(s.get("id"))
        if not job:
            errors.append(f"{s.get('id')}: not in this run's candidates")
            continue
        for d in DIMS:
            if not isinstance(s.get("dims", {}).get(d), (int, float)) or not 1 <= s["dims"][d] <= 5:
                errors.append(f"{s['id']}: dims.{d} must be 1 to 5")
        for r in s.get("requirements", []):
            if not isinstance(r, dict) or not r.get("req"):
                errors.append(f"{s['id']}: each requirement needs 'req' text")
                continue
            if r.get("tag") and r["tag"] not in prefs.get("gap_tags", [r["tag"]]):
                errors.append(f"{s['id']}: unknown gap tag '{r['tag']}'")
            if not isinstance(r.get("evidence", []), list):
                errors.append(f"{s['id']}: evidence must be a list of profile line ids")
                continue
            if r.get("status") not in ("met", "partial", "missing"):
                errors.append(f"{s['id']}: requirement status must be met/partial/missing: {r.get('req')}")
            if r.get("status") in ("met", "partial") and not r.get("evidence"):
                errors.append(f"{s['id']}: '{r.get('req')}' marked {r['status']} without evidence")
            for ev in r.get("evidence", []):
                line = profile["lines"].get(ev)
                if not line:
                    errors.append(f"{s['id']}: evidence {ev} is not a profile line")
                elif line["status"] != "confirmed":
                    errors.append(f"{s['id']}: evidence {ev} is needs-proof; it cannot support a requirement")
    if errors:
        raise SystemExit("scores.json rejected:\n  " + "\n  ".join(errors))

    for s in scores:
        job, dims = cands[s["id"]], dict(s["dims"])
        reqs = s.get("requirements", [])
        cap = _skill_cap(reqs)
        if dims["skill"] > cap:
            dims["skill"], s["skill_capped_from"] = cap, s["dims"]["skill"]
        total = sum(dims[d] * w[d] for d in DIMS) / sum(w.values())
        missing_must = [r["req"] for r in reqs if r.get("must", True) and r["status"] == "missing"]
        if missing_must:
            total = min(total, sc["missing_must_cap"])
        total = round(total, 1)
        tier = "apply" if total >= sc["packet_threshold"] else "maybe" if total >= sc["maybe_threshold"] else "skip"
        res = {**s, "dims": dims, "score": total, "tier": tier, "missing_must": missing_must,
               "job": {k: job.get(k) for k in ("employer", "title", "location", "url", "ats", "posted_at",
                                               "salary", "family", "years_asked", "also_locations", "stale_days",
                                               "visa_flag", "injection_flag",
                                               "repost_of_role_first_seen")}}
        results.append(res)
        prev = rows.get(job["id"], {}).get("status", "found")
        keep = prev not in ("found", "shortlisted", "skipped")  # never demote packet_ready, applied, ...
        tracker.upsert(rows, job, today, score=total,
                       status=prev if keep else "shortlisted" if tier != "skip" else "skipped",
                       notes=(s.get("why") or "")[:160])
    tracker.save(rows)
    results.sort(key=lambda r: -r["score"])
    _dump(rd / "scored.json", results)
    return results


def gap_table() -> list[tuple]:
    """Across every run so far: which skills do postings ask for that the profile cannot evidence?"""
    seen, tags = set(), {}
    for f in sorted(config.RUNS.glob("*/scored.json")):
        for s in _load(f, []):
            if s["id"] in seen:
                continue
            seen.add(s["id"])
            for r in s.get("requirements", []):
                t = tags.setdefault(r.get("tag") or "other", Counter())
                t[r["status"]] += 1
    rows = [(t, c["missing"], c["partial"], c["met"]) for t, c in tags.items()]
    return sorted(rows, key=lambda r: -(r[1] + 0.5 * r[2]))


# ------------------------------------------------------------------ digest

def _contact_for(employer: str, prefs: dict) -> str:
    for c in prefs.get("contacts", []):
        if c["employer"].lower() in employer.lower():
            return c["who"]
    return ""


def digest(today: date) -> str:
    prefs, rd = config.prefs(), run_dir(today)
    report = _load(rd / "discover_report.json", {})
    scored = _load(rd / "scored.json", None)
    cands = _load(rd / "candidates.json", [])
    cap = prefs.get("weekly_application_cap", 8)
    out = [f"# Job digest, {today.isoformat()}", ""]
    out.append(f"Read {report.get('postings_read', 0)} postings from "
               f"{sum(1 for h in report.get('health', {}).values() if not h['error'])} employers; "
               f"{report.get('passed_filters', 0)} passed the hard filters; "
               f"{report.get('new_candidates', 0)} were new this run.")
    out.append("")

    def block(r):
        j = r["job"]
        lines = [f"### {r['score']} | {j['employer']}: {j['title']}",
                 f"- {j['location']}" + (f" (also: {', '.join(j['also_locations'])})" if j.get("also_locations") else "")
                 + (f" | posted {j['posted_at']}" if j.get("posted_at") else "")
                 + (f" | pay: {j['salary']}" if j.get("salary") else ""),
                 f"- Apply: {j.get('url', '')}",
                 "- Scores: " + ", ".join(f"{d} {r['dims'][d]}" for d in DIMS)
                 + (f" (skill capped from {r['skill_capped_from']} by the evidence)" if r.get("skill_capped_from") else ""),
                 f"- Why: {r.get('why', '')}"]
        if r["missing_must"]:
            lines.append(f"- Missing must-haves: {'; '.join(r['missing_must'])}")
        who = _contact_for(j["employer"], prefs)
        if who:
            lines.append(f"- Warm path: {who}. Ask for a referral before applying cold.")
        if j.get("repost_of_role_first_seen"):
            lines.append(f"- Repost: this role was first seen {j['repost_of_role_first_seen']}.")
        if j.get("stale_days"):
            lines.append(f"- Stale: open for {j['stale_days']} days. May be evergreen or already filled; check before investing time.")
        if j.get("visa_flag"):
            lines.append(f"- Visa: {j['visa_flag']}")
        if j.get("injection_flag"):
            lines.append("- Warning: the posting text contains instructions aimed at AI tools. Read it yourself.")
        if r.get("legitimacy") and r["legitimacy"] != "ok":
            lines.append(f"- Legitimacy doubt: {r['legitimacy']}")
        return lines + [""]

    if scored is None:
        out += ["## Candidates ranked by keyword pre-score (not yet judged)", "",
                "| Pre | Employer | Role | Location | Years asked | Link |", "|---|---|---|---|---|---|"]
        for j in cands:
            out.append(f"| {j['prescore']} | {j['employer']} | {j['title']} | {j['location'][:40]} | "
                       f"{j['years_asked'] or ''} | [open]({j.get('url', '')}) |")
        out.append("")
    else:
        tiers = {t: [r for r in scored if r["tier"] == t] for t in ("apply", "maybe", "skip")}
        out += [f"## Apply ({len(tiers['apply'])}; weekly cap {cap})", ""]
        for r in tiers["apply"][:cap]:
            out += block(r)
        if not tiers["apply"]:
            out += ["None reached the packet threshold this week. That is the honest result, not a failure "
                    "of the search; see the skills-gap table below.", ""]
        out += [f"## Maybe ({len(tiers['maybe'])}): apply only with a referral or a strong reason", ""]
        for r in tiers["maybe"]:
            out += block(r)
        out += [f"## Skipped after judgement ({len(tiers['skip'])})", ""]
        out += [f"- {r['score']} | {r['job']['employer']}: {r['job']['title']} ({r['job']['location'][:30]}). "
                f"{(r.get('why') or '')[:110]}" for r in tiers["skip"]] + [""]

        judged = {r["id"] for r in scored}
        rest = [j for j in cands if j["id"] not in judged]
        if rest:
            out += [f"## Passed the filters, not yet judged ({len(rest)})", "",
                    "| Pre | Employer | Role | Location | Link |", "|---|---|---|---|---|"]
            out += [f"| {j['prescore']} | {j['employer']} | {j['title']} | {j['location'][:40]} | [open]({j.get('url', '')}) |"
                    for j in rest] + [""]

    if report.get("gone"):
        rows = tracker.load()
        out += ["## Probably closed", "", "Shortlisted roles no longer on the employer's board:", ""]
        out += [f"- {rows[i]['employer']}: {rows[i]['role']}" for i in report["gone"] if i in rows] + [""]

    due = tracker.followups(today)
    out += [f"## Follow-ups due ({len(due)})", ""]
    out += [f"- {r['employer']}: {r['role']} (applied {r['date_applied']})" for r in due] or ["- None."]
    st = tracker.stats()
    out += ["", "## Funnel", "",
            f"- Tracked {st['tracked']}, applied {st['applied']}, positive replies {st['positive_replies']}"
            + (f", reply rate {st['reply_rate']:.0%}" if st["reply_rate"] is not None else "") + "."]
    if st["applied"] >= 30:
        out.append("- 30 applications reached: time to compare scores with replies and adjust the weights.")

    gaps = gap_table()
    if gaps:
        out += ["", "## Skills-gap table (all judged postings so far)", "",
                "What the market asks for that the confirmed profile cannot yet evidence. "
                "The top rows are the best use of study time.", "",
                "| Skill | Missing | Partial | Met |", "|---|---|---|---|"]
        out += [f"| {t} | {m} | {p} | {k} |" for t, m, p, k in gaps[:15]]

    if report.get("drop_reasons"):
        out += ["", "## Dropped by hard filters", ""]
        out += [f"- {k}: {v}" for k, v in report["drop_reasons"].items()]
    stale_src = {k: v["cached"] for k, v in report.get("health", {}).items() if v.get("cached")}
    if stale_src:
        out += ["", "## Sources served from the last good copy", ""]
        out += [f"- {k}: copy from {d}" for k, d in stale_src.items()]
    bad = {k: v for k, v in report.get("health", {}).items() if v["error"]}
    if bad:
        out += ["", "## Sources that failed this run", ""]
        out += [f"- {k} ({v['ats']}): {v['error']}" for k, v in bad.items()]
    searched = [e["name"] for e in config.portals() if e["ats"] == "search"]
    out += ["", "## Not covered by open endpoints", "",
            "Check by saved search or by hand: " + ", ".join(searched) + "."]
    text = "\n".join(out) + "\n"
    (rd / "digest.md").parent.mkdir(parents=True, exist_ok=True)
    (rd / "digest.md").write_text(text, encoding="utf-8")
    (config.ME / "digest.md").write_text(text, encoding="utf-8")
    return text


def packet_dir(job_id: str, today: date) -> Path:
    scored = {s["id"]: s for s in _load(run_dir(today) / "scored.json", [])}
    cands = {j["id"]: j for j in _load(run_dir(today) / "candidates.json", [])}
    if job_id not in cands:
        raise SystemExit(f"{job_id} is not in the {today} run")
    j = cands[job_id]
    slug = re.sub(r"[^a-z0-9]+", "-", f"{j['employer']} {j['title']}".lower()).strip("-")[:70]
    d = config.PACKETS / f"{today.isoformat()}-{slug}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "posting.txt").write_text(
        f"{j['employer']}\n{j['title']}\n{j['location']}\n{j['url']}\n\n{j['description']}\n", encoding="utf-8")
    if job_id in scored:
        _dump(d / "evaluation.json", scored[job_id])
    return d
