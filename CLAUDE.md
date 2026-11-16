# Job Copilot

Weekly job-search pipeline. Mechanical work is Python (`jobagent/`, free endpoints, no API keys);
judgement work (scoring, wording) is done by Claude following `.claude/skills/job-run/SKILL.md`.

- Private data lives in `me/` and is git-ignored while this repository is public. Never commit it,
  never copy its contents into tracked files, commit messages or PR text.
- A human submits every application. No form filling, no logins, no LinkedIn automation.
- CVs may only use `(confirmed)` lines of `me/master_profile.md`; `python -m jobagent check` enforces it.
- Tests: `python -m unittest discover -s tests` (offline). Live check: `python -m jobagent probe`.
- Add an employer in `config/portals.yml`; read the returned titles to confirm the slug is the right company.
