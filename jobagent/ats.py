"""Fetch postings from free, public ATS job-board endpoints.

Every fetcher returns a list of normalized dicts:
  id, employer, title, location, url, ats, posted_at (YYYY-MM-DD or ""),
  description (plain text, may be "" until enrich()), remote (bool), salary (str)
No scraping, no logins, no API keys. These endpoints are undocumented
conveniences for some vendors and can change; failures are reported, not fatal.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (compatible; job-copilot/1.0; personal job search)"
TIMEOUT = 30


class FetchError(Exception):
    pass


def _http(url: str, data: dict | None = None, headers: dict | None = None, retries: int = 2):
    body = json.dumps(data).encode() if data is not None else None
    h = {"User-Agent": UA, "Accept": "application/json"}
    if body:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404, 410, 422):
                raise FetchError(f"HTTP {e.code}") from None
            if e.code == 429:  # rate-limited (Workday does this to cloud IPs): do not hammer
                raise FetchError("HTTP 429 rate-limited; try again later or from another network") from None
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last = str(e)[:120]
        time.sleep(1.5 * (attempt + 1))
    raise FetchError(last or "failed")


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</(p|div|li|h[1-6]|ul|ol|tr)>|<br\s*/?>|<li[^>]*>", re.I)


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)  # Greenhouse double-escapes
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s)
    return s.strip()


def _date(v) -> str:
    """Accept ISO strings or epoch milliseconds; return YYYY-MM-DD or ''."""
    if v in (None, ""):
        return ""
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000, tz=timezone.utc).date().isoformat()
        return str(v)[:10] if re.match(r"\d{4}-\d{2}-\d{2}", str(v)) else ""
    except (ValueError, OverflowError, OSError):
        return ""


def _job(emp, ats, jid, title, location, url, posted="", desc="", remote=False, salary="", detail=None):
    return {
        "id": f"{emp['slug']}:{jid}",
        "employer": emp["name"],
        "employer_slug": emp["slug"],
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "url": url or "",
        "ats": ats,
        "posted_at": posted,
        "description": desc,
        "remote": bool(remote),
        "salary": salary,
        "_detail": detail,  # URL to fetch for the description, if not included
    }


# ---------------------------------------------------------------- fetchers

def greenhouse(emp):
    d = _http(f"https://boards-api.greenhouse.io/v1/boards/{emp['token']}/jobs?content=true")
    out = []
    for j in d.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(_job(emp, "greenhouse", j["id"], j.get("title"), loc, j.get("absolute_url"),
                        _date(j.get("first_published") or j.get("updated_at")),
                        strip_html(j.get("content")), "remote" in loc.lower()))
    return out


def lever(emp):
    host = "api.eu.lever.co" if emp.get("region") == "eu" else "api.lever.co"
    d = _http(f"https://{host}/v0/postings/{emp['token']}?mode=json")
    out = []
    for j in d:
        c = j.get("categories") or {}
        parts = [j.get("descriptionPlain") or strip_html(j.get("description"))]
        for l in j.get("lists") or []:
            parts.append(f"{l.get('text', '')}\n{strip_html(l.get('content'))}")
        parts.append(j.get("additionalPlain") or "")
        sal = j.get("salaryRange") or {}
        salary = f"{sal.get('min')}-{sal.get('max')} {sal.get('currency', '')}".strip() if sal else ""
        out.append(_job(emp, "lever", j["id"], j.get("text"), c.get("location"), j.get("hostedUrl"),
                        _date(j.get("createdAt")), "\n".join(p for p in parts if p).strip(),
                        j.get("workplaceType") == "remote", salary))
    return out


def ashby(emp):
    d = _http(f"https://api.ashbyhq.com/posting-api/job-board/{emp['token']}?includeCompensation=true")
    out = []
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        out.append(_job(emp, "ashby", j["id"], j.get("title"), "; ".join(l for l in locs if l),
                        j.get("jobUrl"), _date(j.get("publishedAt")),
                        j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")),
                        j.get("isRemote"), comp))
    return out


def smartrecruiters(emp):
    out, offset = [], 0
    base = f"https://api.smartrecruiters.com/v1/companies/{emp['token']}/postings"
    while offset < 1000:
        d = _http(f"{base}?limit=100&offset={offset}")
        for j in d.get("content", []):
            l = j.get("location") or {}
            loc = l.get("fullLocation") or ", ".join(x for x in (l.get("city"), l.get("country")) if x)
            out.append(_job(emp, "smartrecruiters", j["id"], j.get("name"), loc,
                            f"https://jobs.smartrecruiters.com/{emp['token']}/{j['id']}",
                            _date(j.get("releasedDate")), "", l.get("remote"),
                            detail=f"{base}/{j['id']}"))
        offset += 100
        if offset >= d.get("totalFound", 0):
            break
    return out


def workable(emp):
    d = _http(f"https://apply.workable.com/api/v1/widget/accounts/{emp['token']}?details=true")
    out = []
    for j in d.get("jobs", []):
        loc = ", ".join(x for x in (j.get("city"), j.get("country")) if x)
        out.append(_job(emp, "workable", j.get("shortcode"), j.get("title"), loc,
                        j.get("url") or j.get("shortlink"), _date(j.get("published_on")),
                        strip_html(j.get("description")), j.get("telecommuting")))
    return out


def recruitee(emp):
    d = _http(f"https://{emp['token']}.recruitee.com/api/offers/")
    out = []
    for j in d.get("offers", []):
        desc = strip_html(j.get("description")) + "\n" + strip_html(j.get("requirements"))
        out.append(_job(emp, "recruitee", j["id"], j.get("title"), j.get("location"),
                        j.get("careers_url"), _date(j.get("published_at") or j.get("created_at")),
                        desc.strip(), j.get("remote")))
    return out


_POSTED = re.compile(r"(\d+)\+?\s+day", re.I)


def _workday_posted(s: str) -> str:
    """'Posted 3 Days Ago' / 'Posted Today' / 'Posted 30+ Days Ago' -> approximate date."""
    if not s:
        return ""
    today = datetime.now(timezone.utc).date()
    low = s.lower()
    if "today" in low:
        return today.isoformat()
    if "yesterday" in low:
        return (today - timedelta(days=1)).isoformat()
    m = _POSTED.search(s)
    return (today - timedelta(days=int(m.group(1)))).isoformat() if m else ""


def workday(emp, searches=("",), max_pages=3):
    """emp needs host (tenant.wdN.myworkdayjobs.com), tenant, site. Searches by keyword
    because large tenants list thousands of roles and return at most 20 per page."""
    base = f"https://{emp['host']}/wday/cxs/{emp['tenant']}/{emp['site']}"
    seen, out = set(), []
    for term in emp.get("searches") or searches:
        for page in range(max_pages):
            d = _http(f"{base}/jobs", {"appliedFacets": emp.get("facets") or {}, "limit": 20,
                                       "offset": page * 20, "searchText": term})
            posts = d.get("jobPostings") or []
            for j in posts:
                path = j.get("externalPath") or ""
                if not path or path in seen:
                    continue
                seen.add(path)
                jid = (j.get("bulletFields") or [path.rsplit("_", 1)[-1]])[0]
                out.append(_job(emp, "workday", jid, j.get("title"), j.get("locationsText"),
                                f"https://{emp['host']}/{emp['site']}{path}",
                                _workday_posted(j.get("postedOn", "")), "",
                                "remote" in (j.get("locationsText") or "").lower(),
                                detail=f"{base}{path}"))
            if len(posts) < 20:
                break
            time.sleep(0.4)
    return out


FETCHERS = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby,
    "smartrecruiters": smartrecruiters, "workable": workable,
    "recruitee": recruitee, "workday": workday,
}


def fetch(emp: dict, searches=None) -> list[dict]:
    fn = FETCHERS.get(emp.get("ats"))
    if not fn:
        raise FetchError(f"no open endpoint for ats '{emp.get('ats')}'")
    if emp["ats"] == "workday" and searches:
        return workday(emp, searches=searches)
    return fn(emp)


def enrich(job: dict) -> dict:
    """Fill in the description for ATSes that need a second call per posting."""
    url = job.get("_detail")
    if job.get("description") or not url:
        return job
    try:
        d = _http(url)
    except FetchError:
        return job
    if job["ats"] == "smartrecruiters":
        secs = (d.get("jobAd") or {}).get("sections") or {}
        job["description"] = "\n".join(
            f"{v.get('title', '')}\n{strip_html(v.get('text'))}" for v in secs.values() if v).strip()
        job["url"] = d.get("postingUrl") or job["url"]
    elif job["ats"] == "workday":
        info = d.get("jobPostingInfo") or {}
        job["description"] = strip_html(info.get("jobDescription"))
        job["url"] = info.get("externalUrl") or job["url"]
        job["posted_at"] = _date(info.get("startDate")) or job["posted_at"]
        if info.get("location") and not job["location"]:
            job["location"] = info["location"]
    return job
