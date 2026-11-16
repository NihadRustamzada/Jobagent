"""Application tracker: one CSV, readable in Excel, diff-friendly in git."""
from __future__ import annotations

import csv
from datetime import date, timedelta

from . import config

COLS = ["id", "date_found", "employer", "role", "location", "source_url", "ats", "score", "status",
        "cv_version", "date_applied", "answers_sent", "follow_up_due", "response", "notes"]
STATUSES = ["found", "shortlisted", "packet_ready", "applied", "replied", "interview", "offer",
            "rejected", "withdrawn", "skipped"]
FOLLOW_UP_DAYS = 8


def load() -> dict[str, dict]:
    if not config.TRACKER.exists():
        return {}
    with open(config.TRACKER, newline="", encoding="utf-8") as f:
        return {r["id"]: r for r in csv.DictReader(f)}


def save(rows: dict[str, dict]) -> None:
    config.TRACKER.parent.mkdir(parents=True, exist_ok=True)
    with open(config.TRACKER, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: (r["date_found"], r["id"])):
            w.writerow({c: r.get(c, "") for c in COLS})


def upsert(rows: dict, job: dict, today: date, **fields) -> dict:
    r = rows.get(job["id"]) or {c: "" for c in COLS} | {
        "id": job["id"], "date_found": today.isoformat(), "employer": job["employer"],
        "role": job["title"], "location": job.get("location", ""), "source_url": job.get("url", ""),
        "ats": job.get("ats", ""), "status": "found"}
    r.update({k: str(v) for k, v in fields.items()})
    rows[job["id"]] = r
    return r


def set_status(job_id: str, status: str, today: date, note: str = "") -> dict:
    if status not in STATUSES:
        raise SystemExit(f"status must be one of: {', '.join(STATUSES)}")
    rows = load()
    matches = [k for k in rows if k == job_id or job_id.lower() in k.lower()
               or job_id.lower() in (rows[k]["employer"] + " " + rows[k]["role"]).lower()]
    if len(matches) != 1:
        raise SystemExit(f"'{job_id}' matches {len(matches)} tracker rows; use the exact id.")
    r = rows[matches[0]]
    r["status"] = status
    if status == "applied":
        r["date_applied"] = today.isoformat()
        r["follow_up_due"] = (today + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
    if status in ("replied", "interview", "offer", "rejected", "withdrawn"):
        r["response"], r["follow_up_due"] = status, ""
    if note:
        r["notes"] = (r["notes"] + "; " if r["notes"] else "") + note
    save(rows)
    return r


def followups(today: date) -> list[dict]:
    return [r for r in load().values()
            if r["status"] == "applied" and r["follow_up_due"] and r["follow_up_due"] <= today.isoformat()]


def stats() -> dict:
    rows = list(load().values())
    sent = [r for r in rows if r["date_applied"]]
    replied = [r for r in sent if r["response"] in ("replied", "interview", "offer")]
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    return {"tracked": len(rows), "applied": len(sent), "positive_replies": len(replied),
            "reply_rate": round(len(replied) / len(sent), 3) if sent else None, "by_status": by}
