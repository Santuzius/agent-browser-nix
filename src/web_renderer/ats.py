"""Job lists straight from the applicant tracking system (ATS) behind a careers page.

Most employers do not host their vacancies themselves: the careers page embeds Personio, Workday, d.vinci and the like, and each of those has a plain JSON, XML or HTML endpoint that lists every opening without a browser. In the job search of 2026-10-07 agents rediscovered these endpoints by hand about 300 times (curl loops, throwaway parsers, guessed tenant names); this module is that knowledge written down once.

Stdlib only, so it also runs outside the Playwright environment. Every adapter returns the same flat dicts: title, location, date, url.
"""

import gzip
import html as htmllib
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor

INSECURE = ssl.create_default_context()
INSECURE.check_hostname = False
INSECURE.verify_mode = ssl.CERT_NONE

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0 Safari/537.36"


class AtsError(Exception):
    pass


def http(url, data=None, headers=None, timeout=25):
    """GET, or POST when data is given (dicts are sent as JSON). Returns (status, final_url, text); HTTP errors are returned, not raised, because a 404 is an answer the caller wants to report."""
    hdrs = {"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8", "Accept-Encoding": "gzip, deflate"}
    if isinstance(data, (dict, list)):
        data = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
        hdrs["Accept"] = "application/json"
    elif isinstance(data, str):
        data = data.encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs)
    for ctx in (None, INSECURE):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.status, r.url, _decode(r.read(), r.headers)
        except urllib.error.HTTPError as e:
            return e.code, url, _decode(e.read() or b"", e.headers)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # Municipal and utility sites often serve incomplete certificate chains (stadtwerke-esslingen.de, stadtwerke-ettlingen.de). We only read public pages, so retry once without verification.
            if ctx is None and isinstance(getattr(e, "reason", None), ssl.SSLError):
                continue
            raise AtsError(f"{url}: {getattr(e, 'reason', e)}") from None


def _decode(raw, headers):
    enc = (headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw)
    charset = headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def get_json(url, data=None, headers=None):
    for attempt in range(3):
        status, _, text = http(url, data, headers)
        # Recruitee and Personio answer bursts with 429; a short pause is enough.
        if status != 429:
            break
        time.sleep(3 * (attempt + 1))
    if status >= 400:
        raise AtsError(f"{url} answered HTTP {status}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise AtsError(f"{url} did not return JSON (wrong tenant/site name?): {text[:120]!r}") from None


def text_of(fragment):
    """Visible text of an HTML fragment, whitespace collapsed."""
    fragment = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", fragment or "")
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def job(title, location="", date="", url="", **extra):
    # ISO timestamps are cut to the day; anything else (Workday's "Vor 3 Tagen ausgeschrieben") is kept as written.
    date = (date or "").strip()
    if re.match(r"\d{4}-\d\d-\d\d[T ]", date):
        date = date[:10]
    return {"title": text_of(title), "location": text_of(location) if isinstance(location, str) else ", ".join(location), "date": date, "url": url, **extra}


def origin(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


# --- adapters -----------------------------------------------------------------------------------------
# Each adapter: url patterns that identify it (named groups become params), optional HTML signatures for custom domains, and fetch(params, query) -> list of jobs. `query` is passed to the ATS when it can search server-side; every result is filtered client-side again anyway.

def personio(p, query):
    base = f"https://{p['tenant']}.jobs.personio.{p.get('tld') or 'de'}"
    status, _, text = http(f"{base}/xml?language=de")
    if status >= 400 or "<workzag-jobs" not in text:
        raise AtsError(f"{base}/xml returned no job feed (HTTP {status}); the tenant name is probably wrong")
    out = []
    for pos in ET.fromstring(text.encode()).iter("position"):
        g = lambda tag: (pos.findtext(tag) or "").strip()
        out.append(job(g("name"), g("office"), g("createdAt"), f"{base}/job/{g('id')}?language=de", department=g("department"), seniority=g("seniority"), schedule=g("schedule")))
    return out


def workday_facets(facets, location):
    """Location facet values whose name matches the location regex. Workday caps every search at 2,000 hits and pages 20 at a time, so filtering a global employer by city client-side would miss most openings; the facets narrow it server-side. City facets win over country facets when both match."""
    rx, hits = re.compile(location, re.I), {}

    def walk(items, param):
        for f in items or []:
            fp = f.get("facetParameter") or param
            if f.get("id") and "location" in (fp or "").lower() and rx.search(f.get("descriptor", "")):
                hits.setdefault(fp, []).append(f["id"])
            walk(f.get("values"), fp)
    walk(facets, None)
    for fp in ("locations", "locationRegionStateProvince", "locationCountry"):
        if fp in hits:
            return {fp: hits[fp]}
    return dict(list(hits.items())[:1])


def workday(p, query):
    host = f"https://{p['tenant']}.{p['wd']}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{p['tenant']}/{p['site']}/jobs"
    facets = {}
    if p.get("location"):
        first = get_json(api, {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": query or ""})
        facets = workday_facets(first.get("facets"), p["location"])
    page = lambda offset: get_json(api, {"appliedFacets": facets, "limit": 20, "offset": offset, "searchText": query or ""})
    first = page(0)
    total = min(first.get("total", 0), 2000)  # Workday reports the total on the first page only and never pages past 2,000
    # Twenty per request: a global employer is 100 requests, so fetch them eight at a time (two minutes sequentially, about fifteen seconds like this).
    with ThreadPoolExecutor(8) as pool:
        pages = [first, *pool.map(page, range(20, total, 20))]
    return [job(j.get("title", ""), j.get("locationsText", ""), j.get("postedOn", ""), f"{host}/{p['site']}{j.get('externalPath', '')}") for d in pages for j in d.get("jobPostings") or []]


def smartrecruiters(p, query):
    out, offset = [], 0
    while offset < 1000:
        qs = urllib.parse.urlencode({k: v for k, v in {"q": query, "limit": 100, "offset": offset}.items() if v})
        d = get_json(f"https://api.smartrecruiters.com/v1/companies/{p['tenant']}/postings?{qs}")
        rows = d.get("content") or []
        for j in rows:
            loc = j.get("location") or {}
            out.append(job(j.get("name", ""), ", ".join(x for x in (loc.get("city"), loc.get("country", "").upper()) if x), j.get("releasedDate", ""), f"https://jobs.smartrecruiters.com/{p['tenant']}/{j.get('id')}", company=(j.get("company") or {}).get("name", ""), level=(j.get("experienceLevel") or {}).get("label", "")))
        offset += 100
        if offset >= d.get("totalFound", 0) or not rows:
            break
    return out


def recruitee(p, query):
    # The careers widget (`new RTWidget({companies: [21997]})`, e.g. on swe.de) names only a numeric company id; the API under that id serves the same offers as the subdomain.
    d = get_json(f"https://api.recruitee.com/c/{p['company']}/careers/offers" if p.get("company") else f"https://{p['tenant']}.recruitee.com/api/offers/")
    return [job(o.get("title", ""), o.get("location") or o.get("city") or "", o.get("published_at") or o.get("created_at") or "", o.get("careers_url", ""), department=o.get("department") or "") for o in d.get("offers", [])]


def dvinci(p, query):
    base = f"https://{p['tenant']}.dvinci-hr.com"
    d = get_json(f"{base}/jobPublication/list.json?lang=de")
    out = []
    for j in d if isinstance(d, list) else []:
        locs = [x.get("name") or x.get("city") or "" for x in ((j.get("jobOpening") or {}).get("locations") or [])]
        out.append(job(j.get("position", ""), locs, j.get("startDate") or "", j.get("jobPublicationURL", "")))
    return out


def softgarden(p, query):
    """Custom-domain softgarden sites serve a schema.org feed at /jobs.feed.json; the *.softgarden.io hosts do not (404), but list every opening on /de/vacancies."""
    base = p.get("base") or f"https://{p['tenant']}.softgarden.io"
    try:
        d = get_json(f"{base}/jobs.feed.json")
    except AtsError:
        status, _, text = http(f"{base}/de/vacancies")
        if status >= 400:
            raise AtsError(f"{base}: neither /jobs.feed.json nor /de/vacancies answered (HTTP {status})")
        return _links(f"{base}/de/vacancies", text, r"job/\d+")
    out = []
    for el in d.get("dataFeedElement", []):
        it = el.get("item") or {}
        loc = it.get("jobLocation") or {}
        loc = loc[0] if isinstance(loc, list) and loc else loc
        addr = (loc.get("address") or {}) if isinstance(loc, dict) else {}
        out.append(job(it.get("title", ""), addr.get("addressLocality", ""), it.get("datePosted", ""), it.get("url", "")))
    return out


def lever(p, query):
    d = get_json(f"https://api.lever.co/v0/postings/{p['tenant']}?mode=json")
    import datetime
    return [job(j.get("text", ""), (j.get("categories") or {}).get("location", ""), datetime.date.fromtimestamp(j["createdAt"] / 1000).isoformat() if j.get("createdAt") else "", j.get("hostedUrl", "")) for j in d]


def greenhouse(p, query):
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{p['tenant']}/jobs")
    return [job(j.get("title", ""), (j.get("location") or {}).get("name", ""), j.get("updated_at", ""), j.get("absolute_url", "")) for j in d.get("jobs", [])]


SF_ROW = re.compile(r'<tr class="data-row".*?</tr>', re.S)


def successfactors(p, query):
    """SAP SuccessFactors Career Site Builder (jobs.fraunhofer.de, sma.jobs, *.jobs.hr.cloud.sap, mainova-karriere.de). Server-rendered search pages, 25 rows each."""
    base, out, start = p["base"], [], 0
    while start < 1000:
        qs = urllib.parse.urlencode({"q": query or "", "sortColumn": "referencedate", "sortDirection": "desc", "startrow": start})
        status, _, text = http(f"{base}/search/?{qs}")
        if status >= 400:
            raise AtsError(f"{base}/search/ answered HTTP {status}")
        rows = SF_ROW.findall(text)
        for r in rows:
            m = re.search(r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*jobTitle-link[^"]*"[^>]*>(.*?)</a>', r, re.S) or re.search(r'<a[^>]+class="[^"]*jobTitle-link[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r, re.S)
            if not m:
                continue
            loc = re.search(r'class="jobLocation[^"]*"[^>]*>(.*?)</span>', r, re.S)
            date = re.search(r'class="jobDate[^"]*"[^>]*>(.*?)</span>', r, re.S)
            out.append(job(m.group(2), loc.group(1) if loc else "", text_of(date.group(1)) if date else "", urllib.parse.urljoin(base, m.group(1))))
        if len(rows) < 25:
            break
        start += 25
    # A row usually appears twice (desktop and mobile layout).
    seen, uniq = set(), []
    for j in out:
        if j["url"] not in seen:
            seen.add(j["url"])
            uniq.append(j)
    return uniq


def eightfold(p, query):
    base, out, start = p["base"], [], 0
    while start < 500:
        qs = urllib.parse.urlencode({k: v for k, v in {"domain": p["domain"], "query": query or "", "location": p.get("location", ""), "start": start, "num": 10}.items() if v != ""})
        d = (get_json(f"{base}/api/pcsx/search?{qs}").get("data") or {})
        rows = d.get("positions") or []
        import datetime
        out += [job(j.get("name", ""), j.get("locations") or [], datetime.date.fromtimestamp(j["postedTs"]).isoformat() if j.get("postedTs") else "", f"{base}/careers/job/{j.get('id')}") for j in rows]
        start += 10
        if not rows or start >= d.get("count", 0):
            break
    return out


def _links(base, html, href_re):
    out, seen = [], set()
    for m in re.finditer(r'<a\b[^>]*href="([^"]*' + href_re + r'[^"]*)"[^>]*>(.*?)</a>', html, re.S | re.I):
        url = urllib.parse.urljoin(base, htmllib.unescape(m.group(1)))
        title = text_of(m.group(2))
        if url in seen or len(title) < 4:
            continue
        seen.add(url)
        out.append(job(title, url=url))
    return out


def dvvbw(p, query):
    """Kommunales Bewerberportal of the Datenverarbeitungsverbund BW (`<org>.bewerbung.dvvbw.de`), also used under other hosts such as ppa-duew.jobs."""
    base, out = p["base"], []
    for page in range(1, 20):
        status, _, text = http(f"{base}/index?page={page}" if page > 1 else f"{base}/index")
        if status >= 400:
            if page == 1:
                raise AtsError(f"{base}/index answered HTTP {status}")
            break
        rows = [j for j in _links(base, text, r"/job\.html") if j["url"] not in {x["url"] for x in out}]
        if not rows:
            break
        out += rows
    return out


def meincheckin(p, query):
    base = f"https://www.mein-check-in.de/{p['tenant']}"
    status, _, text = http(f"{base}/index")
    if status >= 400:
        status, _, text = http(base)
    if status >= 400:
        raise AtsError(f"{base} answered HTTP {status}")
    return _links("https://www.mein-check-in.de/", text, r"/position-\d+")


def guidecom(p, query):
    base = f"https://connect.guidecom.de/jobportal/{p['tenant']}"
    status, _, text = http(f"{base}/viewAusschreibungen.html")
    if status >= 400:
        raise AtsError(f"{base}/viewAusschreibungen.html answered HTTP {status}")
    return _links(base + "/", text, r"viewAusschreibung/")


ADAPTERS = {
    "personio": (personio, [r"https?://(?P<tenant>[\w-]+)\.jobs\.personio\.(?P<tld>de|com)"]),
    "workday": (workday, [r"https?://(?P<tenant>[\w-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[\w-]+/)?(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>(?!wday\b)[\w-]+)"]),
    "smartrecruiters": (smartrecruiters, [r"https?://(?:jobs|careers)\.smartrecruiters\.com/(?P<tenant>[\w-]+)", r"api\.smartrecruiters\.com/v1/companies/(?P<tenant>[\w-]+)"]),
    "recruitee": (recruitee, [r"https?://(?P<tenant>(?!api\b)[\w-]+)\.recruitee\.com", r"RTWidget\(\{\s*companies:\s*\[(?P<company>\d+)", r"api\.recruitee\.com/c/(?P<company>\d+)"]),
    "dvinci": (dvinci, [r"https?://(?P<tenant>[\w-]+)\.dvinci-hr\.com"]),
    "softgarden": (softgarden, [r"https?://(?P<tenant>[\w-]+)\.softgarden\.io"]),
    "lever": (lever, [r"https?://jobs\.lever\.co/(?P<tenant>[\w.-]+)", r"api\.lever\.co/v0/postings/(?P<tenant>[\w.-]+)"]),
    "greenhouse": (greenhouse, [r"https?://(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:v1/boards/|embed/job_board\?for=)?(?P<tenant>[\w-]+)"]),
    "dvvbw": (dvvbw, [r"(?P<base>https?://[\w-]+\.bewerbung\.dvvbw\.de)", r"(?P<base>https?://[\w-]+\.ppa-duew\.jobs)"]),
    "mein-check-in": (meincheckin, [r"https?://(?:www\.)?mein-check-in\.de/(?P<tenant>(?!x/|index|static|assets)[\w-]+)"]),
    "guidecom": (guidecom, [r"https?://(?:connect|jobs)\.guidecom\.de/jobportal/(?P<tenant>[\w-]+)"]),
    # Custom-domain systems, recognised by HTML signatures in detect(); the page origin becomes `base`.
    "successfactors": (successfactors, [r"(?P<base>https?://[\w.-]+\.jobs\.hr\.cloud\.sap)"]),
    "eightfold": (eightfold, []),
}

# HTML signatures of systems that run under the employer's own domain. Value: adapter name, or None for systems with no adapter yet (reported so the agent knows what it is looking at).
SIGNATURES = [
    (r"rmkcdn\.successfactors\.com|career\d*\.successfactors\.(?:eu|com)", "successfactors"),
    (r"eightfold\.ai|/api/pcsx/", "eightfold"),
    (r"softgarden", "softgarden"),
    (r"phenompeople|phenom-?(?:cdn|widgets)|\bphApp\b", None),
    (r"static\.b-ite\.com|data-bite-jobs-api", None),
    (r"umantis\.com", None),
    (r"rexx-systems|/stellenangebote\.html\?start=|-j\d+\.html", None),
    (r"\bavature\b", None),
    (r"onlyfy\.jobs|onlyfy", None),
    (r"interamt\.de", None),
    (r"helixjobs\.com", None),
    (r"coveto\.de", None),
    (r"hrworks\.de", None),
    (r"myjobboard\.de", None),
    (r"join\.com/companies", None),
    (r"recruiting-portal\.com", None),
]

NOTES = {
    "phenompeople": "Phenom: list via POST {origin}/widgets with the page's refineSearch payload (see README); render_page on the search-results page shows only the first page",
    "b-ite": "b-ite: rows open by click, no list API found; render_page with click_rows",
    "umantis": "umantis: list /Jobs/All, details /Vacancies/{id}/Description/1 hold the text in an iframe; render_page reads both",
    "rexx": "rexx: list {origin}/stellenangebote.html?start=0,15,30…; RSS rss_generator-rss0.php",
    "avature": "Avature (e.g. Siemens): {origin}/{locale}/externaljobs/SearchJobs/{q}/?folderRecordsPerPage=50&folderOffset=N",
    "interamt": "interamt: details /koop/app/stelle?id=N need a browser session (render_page); the search form ignores URL parameters",
}


def detect(url, html=""):
    """Every ATS that the URL or the page's HTML points to, as a list of (name, params, how). An embed on a careers page shows up as a URL inside the HTML, so the same URL patterns run over both."""
    found, seen = [], set()
    for name, (_, patterns) in ADAPTERS.items():
        for pat in patterns:
            for src, text in (("url", url), ("html", html)):
                for m in re.finditer(pat, text or ""):
                    params = {k: v for k, v in m.groupdict().items() if v}
                    key = (name, tuple(sorted(params.items())))
                    if key not in seen and params.get("tenant", "x").lower() not in ("www", "api", "static", "cdn", "assets", "jobs"):
                        seen.add(key)
                        found.append((name, params, src))
    for sig, name in SIGNATURES:
        if html and re.search(sig, html, re.I):
            label = name or re.sub(r"\\.|\(.*", "", sig.split("|")[0]).replace("\\", "").replace("static.", "")
            if name and not any(f[0] == name for f in found):
                params = {"base": origin(url)}
                if name == "eightfold":
                    m = re.search(r'["\']domain["\']\s*:\s*["\']([\w.-]+)["\']', html) or re.search(r"domain=([\w.-]+)", html)
                    params["domain"] = m.group(1) if m else urllib.parse.urlsplit(url).netloc.split(".", 1)[-1]
                found.append((name, params, "html signature"))
            elif not name:
                found.append((label, {}, "html signature, no adapter"))
    return found


def fetch_jobs(name, params, query="", location=""):
    """`location` is passed on where the ATS can narrow by place server-side (Workday location facets, Eightfold); filter_jobs applies it again either way."""
    if name not in ADAPTERS:
        raise AtsError(f"no adapter for {name}; supported: {', '.join(ADAPTERS)}")
    return ADAPTERS[name][0](dict(params, location=location), query)


def filter_jobs(jobs, query="", location="", exclude=""):
    """Case-insensitive regex filters over title (+ department/company) and location. Plain words work as regexes too."""
    q = re.compile(query, re.I) if query else None
    loc = re.compile(location, re.I) if location else None
    ex = re.compile(exclude, re.I) if exclude else None
    out = []
    for j in jobs:
        hay = " ".join(str(j.get(k, "")) for k in ("title", "department", "company"))
        if q and not q.search(hay):
            continue
        # The URL counts too: SuccessFactors and Workday put the city in the job URL (/job/Freiburg-…) even where the list shows none.
        if loc and not loc.search(" ".join((j.get("location", ""), j.get("title", ""), urllib.parse.unquote(j.get("url", ""))))):
            continue
        if ex and ex.search(j["title"]):
            continue
        out.append(j)
    return out


def format_jobs(jobs):
    return "\n".join("- " + " | ".join(x for x in (j["title"], j.get("location", ""), j.get("date", ""), j.get("url", "")) if x) for j in jobs)
