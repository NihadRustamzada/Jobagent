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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (compatible; job-copilot/1.0; personal job search)"
TIMEOUT = 30
MAX_BYTES = 60_000_000  # refuse to buffer an absurd response


class FetchError(Exception):
    pass


_SLOW_HOSTS = {"apply.workable.com": 10.0, "myworkdayjobs.com": 1.5}  # seconds between calls; these throttle bursts
_locks: dict[str, threading.Lock] = {}
_last: dict[str, float] = {}
_guard = threading.Lock()


def _pace(host: str):
    """(lock, gap): one request at a time, spaced out, for hosts that rate-limit bursts."""
    gap = next((g for h, g in _SLOW_HOSTS.items() if host.endswith(h)), None)
    if gap is None:
        return None, 0.0
    with _guard:
        return _locks.setdefault(host, threading.Lock()), gap


def _http(url: str, data: dict | None = None, headers: dict | None = None, retries: int = 2):
    body = json.dumps(data).encode() if data is not None else None
    h = {"User-Agent": UA, "Accept": "application/json"}
    if body:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    host = urllib.parse.urlsplit(url).netloc
    (lock, gap), last = _pace(host), None
    for attempt in range(retries + 1):
        wait = 1.5 * attempt
        try:
            if lock:
                with lock:
                    time.sleep(max(0.0, gap - (time.monotonic() - _last.get(host, 0.0))))
                    try:
                        return _open(url, body, h)
                    finally:
                        _last[host] = time.monotonic()
            return _open(url, body, h)
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404, 410, 422):
                raise FetchError(f"HTTP {e.code}") from None
            last = f"HTTP {e.code}"
            if e.code == 429:
                last = "HTTP 429 rate-limited; try again later or from another network"
                try:
                    wait = min(30.0, float(e.headers.get("Retry-After") or 8 * (attempt + 1)))
                except ValueError:
                    wait = 6.0 * (attempt + 1)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            last = str(e)[:120]
        if attempt < retries:
            time.sleep(wait)
    raise FetchError(last or "failed")


def _open(url, body, h):
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read(MAX_BYTES).decode("utf-8", "replace"))


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</(p|div|li|h[1-6]|ul|ol|tr)>|<br\s*/?>|<li[^>]*>", re.I)


def strip_html(s: str | None) -> str:
    if not s or not isinstance(s, str):
        return ""
    s = html.unescape(s)  # Greenhouse double-escapes
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s)
    return s.strip()


def _s(v) -> str:
    return v if isinstance(v, str) else ""


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
    title, location = _s(title) if not isinstance(title, str) else title, location if isinstance(location, str) else ""
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
# Each board has a row mapper (one posting -> normalized job). `_rows` and `_map` make the
# fetchers tolerant: an unexpected payload is a FetchError, one malformed posting is skipped.

def _rows(d, key=None) -> list[dict]:
    rows = d.get(key) if key and isinstance(d, dict) else d if key is None else None
    if not isinstance(rows, list):
        raise FetchError("unexpected response shape")
    return [r for r in rows if isinstance(r, dict)]


def _map(rows, fn) -> list[dict]:
    out = []
    for r in rows:
        try:
            j = fn(r)
        except (KeyError, TypeError, AttributeError, ValueError):
            continue
        if j and j["title"] and not j["id"].endswith(":None"):
            out.append(j)
    return out


def greenhouse(emp):
    def row(j):
        loc = _s((j.get("location") or {}).get("name"))
        return _job(emp, "greenhouse", j["id"], j.get("title"), loc, _s(j.get("absolute_url")),
                    _date(j.get("first_published") or j.get("updated_at")),
                    strip_html(_s(j.get("content"))), "remote" in loc.lower())
    return _map(_rows(_http(f"https://boards-api.greenhouse.io/v1/boards/{emp['token']}/jobs?content=true"), "jobs"), row)


def lever(emp):
    host = "api.eu.lever.co" if emp.get("region") == "eu" else "api.lever.co"

    def row(j):
        c = j.get("categories") or {}
        parts = [_s(j.get("descriptionPlain")) or strip_html(_s(j.get("description")))]
        for l in j.get("lists") or []:
            parts.append(f"{_s(l.get('text'))}\n{strip_html(_s(l.get('content')))}")
        parts.append(_s(j.get("additionalPlain")))
        sal = j.get("salaryRange") or {}
        salary = f"{sal.get('min')}-{sal.get('max')} {sal.get('currency', '')}".strip() if sal else ""
        return _job(emp, "lever", j["id"], j.get("text"), _s(c.get("location")), _s(j.get("hostedUrl")),
                    _date(j.get("createdAt")), "\n".join(p for p in parts if p).strip(),
                    j.get("workplaceType") == "remote", salary)
    return _map(_rows(_http(f"https://{host}/v0/postings/{emp['token']}?mode=json")), row)


def ashby(emp):
    def row(j):
        if j.get("isListed") is False:
            return None
        locs = [_s(j.get("location"))] + [_s(x.get("location")) for x in j.get("secondaryLocations") or []]
        comp = _s((j.get("compensation") or {}).get("compensationTierSummary"))
        return _job(emp, "ashby", j["id"], j.get("title"), "; ".join(l for l in locs if l),
                    _s(j.get("jobUrl")), _date(j.get("publishedAt")),
                    _s(j.get("descriptionPlain")) or strip_html(_s(j.get("descriptionHtml"))),
                    j.get("isRemote"), comp)
    url = f"https://api.ashbyhq.com/posting-api/job-board/{emp['token']}?includeCompensation=true"
    return _map(_rows(_http(url), "jobs"), row)


def smartrecruiters(emp):
    out, offset = [], 0
    base = f"https://api.smartrecruiters.com/v1/companies/{emp['token']}/postings"

    def row(j):
        l = j.get("location") or {}
        loc = _s(l.get("fullLocation")) or ", ".join(x for x in (_s(l.get("city")), _s(l.get("country"))) if x)
        return _job(emp, "smartrecruiters", j["id"], j.get("name"), loc,
                    f"https://jobs.smartrecruiters.com/{emp['token']}/{j['id']}",
                    _date(j.get("releasedDate")), "", l.get("remote"), detail=f"{base}/{j['id']}")
    while offset < 1000:
        d = _http(f"{base}?limit=100&offset={offset}")
        out += _map(_rows(d, "content"), row)
        offset += 100
        total = d.get("totalFound")
        if not isinstance(total, int) or offset >= total:
            break
    return out


def workable(emp):
    def row(j):
        loc = ", ".join(x for x in (_s(j.get("city")), _s(j.get("country"))) if x)
        return _job(emp, "workable", j.get("shortcode"), j.get("title"), loc,
                    _s(j.get("url")) or _s(j.get("shortlink")), _date(j.get("published_on")),
                    strip_html(_s(j.get("description"))), j.get("telecommuting"))
    url = f"https://apply.workable.com/api/v1/widget/accounts/{emp['token']}?details=true"
    return _map(_rows(_http(url), "jobs"), row)


def recruitee(emp):
    def row(j):
        desc = strip_html(_s(j.get("description"))) + "\n" + strip_html(_s(j.get("requirements")))
        return _job(emp, "recruitee", j["id"], j.get("title"), _s(j.get("location")),
                    _s(j.get("careers_url")), _date(j.get("published_at") or j.get("created_at")),
                    desc.strip(), j.get("remote"))
    return _map(_rows(_http(f"https://{emp['token']}.recruitee.com/api/offers/"), "offers"), row)


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

    def row(j):
        path = _s(j.get("externalPath"))
        if not path or path in seen:
            return None
        seen.add(path)
        loc = _s(j.get("locationsText"))
        jid = (j.get("bulletFields") or [path.rsplit("_", 1)[-1]])[0]
        return _job(emp, "workday", jid, j.get("title"), loc, f"https://{emp['host']}/{emp['site']}{path}",
                    _workday_posted(_s(j.get("postedOn"))), "", "remote" in loc.lower(),
                    detail=f"{base}{path}")
    for term in emp.get("searches") or searches:
        for page in range(max_pages):
            d = _http(f"{base}/jobs", {"appliedFacets": emp.get("facets") or {}, "limit": 20,
                                       "offset": page * 20, "searchText": term})
            posts = _rows(d, "jobPostings")
            out += _map(posts, row)
            if len(posts) < 20:
                break
            time.sleep(1.0)
        time.sleep(1.0)  # Workday rate-limits bursts
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
    if not isinstance(d, dict):
        return job
    if job["ats"] == "smartrecruiters":
        secs = (d.get("jobAd") or {}).get("sections") or {}
        job["description"] = "\n".join(
            f"{_s(v.get('title'))}\n{strip_html(_s(v.get('text')))}" for v in secs.values() if isinstance(v, dict)).strip()
        job["url"] = d.get("postingUrl") or job["url"]
    elif job["ats"] == "workday":
        info = d.get("jobPostingInfo") or {}
        job["description"] = strip_html(info.get("jobDescription"))
        job["url"] = info.get("externalUrl") or job["url"]
        job["posted_at"] = _date(info.get("startDate")) or job["posted_at"]
        if info.get("location") and not job["location"]:
            job["location"] = info["location"]
    return job
