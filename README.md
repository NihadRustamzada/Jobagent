# Job Copilot

A weekly job-search pipeline that finds roles, filters and scores them against an honest
profile, and prepares a ready-to-send packet for each good one. **You press Submit yourself.**

It costs nothing to run: postings come from free public job-board endpoints, the code needs
two small Python packages, and the judgement steps run inside a Claude subscription you
already have. No API keys, no paid scrapers, no browser automation.

## How it works

```
config/portals.yml  ─┐
me/preferences.yml  ─┤  1 DISCOVER   free ATS endpoints (Greenhouse, Lever, Ashby,        code
me/master_profile.md ┤               SmartRecruiters, Workable, Recruitee, Workday)
me/answer_bank.md   ─┘  2 FILTER     seniority, location, language, citizens-only, age    code
                        3 JUDGE      each requirement: met / partial / missing + evidence  Claude
                        4 SCORE      weighted 1 to 5, capped by the evidence               code
                        5 PACKET     tailored CV, cover note, screening answers            Claude
                        6 CHECK      anti-fabrication check, then render cv.docx           code
                        7 TRACK      tracker.csv, follow-ups, digest, skills-gap table     code
```

Design choices that matter:

- **The score is computed by code, not asserted by the model.** The model fills a requirement
  table citing profile line IDs; code rejects evidence that is missing or unconfirmed, caps the
  skill score by that table, and keeps any job with a missing must-have out of the "apply" tier.
- **CVs are assembled, not written.** Employer, title, dates and education come verbatim from
  the profile. Only bullet wording is tailored, every bullet cites its source lines, and a
  mechanical check fails the CV on any number, name, tool or strength word its sources lack.
- **Unproven claims are locked.** Lines marked `needs-proof` cannot be cited, and their wording
  cannot leak into a CV.
- **The digest is also a market sensor.** Every judged requirement is tagged, so the digest shows
  which skills postings keep asking for that the profile cannot yet evidence.
- **Postings are untrusted input.** Instructions embedded in a job description are ignored.

## Setup

```bash
pip install -r requirements.txt
cp -r me.example me          # then edit the four files in me/
python -m jobagent profile   # lists lines still marked needs-proof
python -m jobagent probe     # tests every endpoint in config/portals.yml
```

`me/` holds everything personal and is git-ignored. Keep it that way while the repository is
public. If you make the repository private, delete the `me/` line in `.gitignore` to version
every CV you send and to let scheduled runs find your data.

## Weekly run

With Claude Code in this folder: ask it to **run the weekly job run** (skill `job-run`).
By hand, the mechanical parts are:

```bash
python -m jobagent discover               # writes me/runs/<date>/candidates.json
python -m jobagent digest                 # ranked by keyword pre-score if nothing is judged yet
python -m jobagent score-apply            # after scores.json exists
python -m jobagent packet <job id>        # creates me/packets/<date>-<employer>-<role>/
python -m jobagent render <packet folder> # check, then cv.docx + cv_preview.md
python -m jobagent track "<employer>" applied
python -m jobagent followups
python -m jobagent stats
```

## Files in `me/`

| File | Purpose |
|---|---|
| `master_profile.md` | Every fact with an ID and a status: `(confirmed)` or `(needs-proof: reason)` |
| `answer_bank.md` | True answers to recurring form questions; gaps become `NEEDS YOU` in packets |
| `preferences.yml` | Role families, locations and pay floors, exclusions, contacts, scoring weights |
| `contact.yml` | Name, phone, email for the CV header only |
| `tracker.csv`, `digest.md`, `runs/`, `packets/` | Written by the pipeline |

`cv.json` shape (written per packet, checked before rendering):

```json
{"summary": {"text": "...", "sources": ["E4", "D12"]},
 "experience": [{"entry": "X1", "bullets": [{"text": "...", "sources": ["E4"]}]}],
 "projects": [{"text": "...", "sources": ["P1"]}],
 "education": ["D1", "D3"], "education_lines": {"D1": ["D11"]},
 "skills": [{"text": "...", "sources": ["S2"]}]}
```

## Limits

- Open endpoints are conveniences, not contracts, and can change. `probe` shows what broke.
- Workday rate-limits cloud IP addresses (HTTP 429); it works more often from a home connection.
- Oracle, SuccessFactors, Taleo and custom portals have no open endpoint. Those employers are
  listed as `ats: search` and covered by web searches during the judged run, or by hand.
- The fabrication check reduces errors; it cannot catch every rewording. Read every CV before sending.
- This finds and prepares. It does not make anyone a stronger candidate.

## Not in scope, on purpose

Auto-submitting, form filling, LinkedIn automation, CAPTCHA or bot-detection bypass, stored
portal passwords.
