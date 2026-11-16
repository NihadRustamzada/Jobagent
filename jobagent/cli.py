"""Command line: python -m jobagent <command>."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import ats, check, config, pipeline, profile as prof, render, tracker


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="jobagent", description=__doc__)
    p.add_argument("--date", help="run date, YYYY-MM-DD (default: today)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="test every open endpoint in portals.yml")
    d = sub.add_parser("discover", help="fetch, filter, pre-score; writes candidates.json")
    d.add_argument("--all", action="store_true", help="include jobs already judged in earlier runs")
    d.add_argument("--only", help="only employers whose name contains this text")
    sub.add_parser("score-apply", help="validate scores.json and compute final scores")
    sub.add_parser("digest", help="write the weekly digest")
    sub.add_parser("profile", help="summarise the master profile and its unconfirmed lines")
    k = sub.add_parser("packet", help="create the packet folder for a job id")
    k.add_argument("job_id")
    c = sub.add_parser("check", help="anti-fabrication check on a packet folder")
    c.add_argument("packet")
    r = sub.add_parser("render", help="check, then render cv.json to cv.docx")
    r.add_argument("packet")
    t = sub.add_parser("track", help="set a status: track <id or text> <status> [note]")
    t.add_argument("job_id")
    t.add_argument("status", choices=tracker.STATUSES)
    t.add_argument("note", nargs="?", default="")
    sub.add_parser("followups", help="applications due a follow-up")
    sub.add_parser("stats", help="funnel numbers")
    a = p.parse_args(argv)
    today = date.fromisoformat(a.date) if a.date else date.today()
    profile_path = config.me_file("master_profile.md")

    if a.cmd == "probe":
        bad = 0
        for emp in config.portals():
            if emp["ats"] == "search":
                continue
            try:
                jobs = ats.fetch(emp, ["risk"])
                print(f"ok    {emp['name']:<32} {emp['ats']:<16} {len(jobs):>4} postings")
            except Exception as e:
                bad += 1
                print(f"FAIL  {emp['name']:<32} {emp['ats']:<16} {e}")
        return 1 if bad else 0
    if a.cmd == "discover":
        rep = pipeline.discover(today, a.all, a.only)
        print(json.dumps({k: v for k, v in rep.items() if k != "health"}, indent=1))
        fails = [k for k, v in rep["health"].items() if v["error"]]
        if fails:
            print("failed sources:", ", ".join(fails))
    elif a.cmd == "score-apply":
        for r in pipeline.apply_scores(today):
            print(f"{r['score']:.1f}  {r['tier']:<5}  {r['job']['employer']}: {r['job']['title']}")
    elif a.cmd == "digest":
        pipeline.digest(today)
        print(f"wrote {config.ME / 'digest.md'}")
    elif a.cmd == "profile":
        print(prof.summary(prof.load(profile_path)))
    elif a.cmd == "packet":
        print(pipeline.packet_dir(a.job_id, today))
    elif a.cmd in ("check", "render"):
        ok, text = check.run(Path(a.packet), profile_path)
        print(text)
        if not ok:
            print("\nFAILED: fix cv.json and run again. Nothing was rendered.")
            return 1
        if a.cmd == "render":
            for f in render.render(Path(a.packet), profile_path):
                print("wrote", f)
    elif a.cmd == "track":
        r = tracker.set_status(a.job_id, a.status, today, a.note)
        print(f"{r['employer']}: {r['role']} -> {r['status']}"
              + (f" (follow up by {r['follow_up_due']})" if r["follow_up_due"] else ""))
    elif a.cmd == "followups":
        for r in tracker.followups(today):
            print(f"{r['follow_up_due']}  {r['employer']}: {r['role']}  {r['source_url']}")
    elif a.cmd == "stats":
        print(json.dumps(tracker.stats(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
