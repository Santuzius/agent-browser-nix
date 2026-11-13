"""MCP server over stdio (`web-renderer mcp`): render pages in headless Chromium (in the agent Vivaldi after a bot check), find an employer's careers page and read job lists straight from the applicant tracking system behind it.

Why it exists: fetcher-mcp (Readability over one page) drops list items, ignores load-more buttons, pagination, iframes and forms, and a job search with a dozen agents on 2026-10-07 spent most of its ~1,400 tool calls working around that with render-page.py over Bash, curl loops and throwaway parsers. The same capabilities are tools here, so agents without Bash can use them and nobody rewrites a Workday client again. Requests are handled concurrently: one Chromium, a fresh context per call, at most RENDERER_CONCURRENCY pages at once.

Started by __main__.py, which also sets up the Python environment.
"""

import asyncio
import json
import os
import re
import sys
import urllib.parse

from . import ats
from .extraction import LINKS
from .headless import UA
from .markdown import format_markdown
from .page_actions import settle
from .render import Renderer

PROTOCOL_VERSION = "2025-06-18"
BROWSER = Renderer(concurrency=int(os.environ.get("RENDERER_CONCURRENCY", "4")))

RENDER_PROPS = {
    "mode": {"type": "string", "enum": ["auto", "defuddle", "readability", "full", "html"], "description": "Extractor. auto (default) picks the best of Defuddle/Readability/full; full = whole page minus nav and banners, best for lists; readability = single article or job ad."},
    "grep": {"type": "string", "description": "Return only lines matching this regex (case-insensitive), each with the heading above it. E.g. 'm/w/d|w/m/d' on a job list."},
    "exclude": {"type": "string", "description": "Drop lines matching this regex, e.g. 'Ausbildung|Praktikum|Werkstudent'."},
    "context": {"type": "integer", "description": "Lines of context around grep hits (default 0)."},
    "links": {"type": "string", "description": "Also return the page's links whose text or URL matches this regex ('.' for all)."},
    "links_only": {"type": "boolean", "description": "Return only the (filtered) links, no page text. For finding sub-pages."},
    "max_length": {"type": "integer", "description": "Characters per part (default 20000, 0 = all). Longer pages are cut at paragraph breaks into parts 1/n; the footer names the next part."},
    "part": {"type": "integer", "description": "Which part to return (1-based), e.g. 2 after a footer '[part 1/3 …; next: part=2]'. The page is rendered again, so prefer grep for a single fact."},
    "start": {"type": "integer", "description": "Character offset, an alternative to part."},
    "full_urls": {"type": "boolean", "description": "Keep link targets longer than 100 characters (shortened to host/path/… by default; tracking and redirect links otherwise eat half the output)."},
    "browser": {"type": "string", "enum": ["auto", "headless", "agent"], "description": "auto (default): headless Chromium, and the agents' visible Vivaldi (logged in, passes Cloudflare) if headless hits a bot check. agent: always Vivaldi, e.g. for pages behind the user's login; pages/fill/select/click_rows/iframes are not available there."},
    "accept_cookies": {"type": "boolean", "description": "Click an 'Akzeptieren'/'Accept' consent button first (any frame). Needed when content stays hidden or a backdrop blocks clicks."},
    "wait": {"type": "integer", "description": "Milliseconds to wait for the network to go idle after load and after each click (default 8000). Values below 100 are read as seconds."},
}

TOOLS = [
    {
        "name": "render_page",
        "description": "Render one page in headless Chromium and return it as Markdown. Use for JavaScript pages, job portals and lists (web_url_read sees no JavaScript; fetch_url drops list items). Clicks load-more buttons automatically ('Mehr laden', 'Load more', …; expand_pattern for others), reads iframes and web components, opens PDFs. Can also follow pagination (pages), fill and submit a search form (fill/select/submit), and open rows that have no links (click_rows). Returns Note: lines saying what it did; read them.",
        "inputSchema": {"type": "object", "required": ["url"], "properties": {
            "url": {"type": "string"},
            **RENDER_PROPS,
            "pages": {"type": "integer", "description": "Follow pagination up to page N ('Weiter', '›', rel=next, else the button labelled with the next number). Each page follows as '## Seite N'."},
            "expand": {"type": "integer", "description": "Click load-more buttons up to N times (default 10, 0 = off); stops when a click adds no text."},
            "expand_pattern": {"type": "string", "description": "Texts of load-more buttons that grow the list in place, separated by | (case-insensitive, whole button text), e.g. 'Weitere Stellen|Mehr Treffer|Alle Jobs anzeigen'. Default covers Mehr laden, Mehr anzeigen, Weitere laden/anzeigen/Ergebnisse, Alle anzeigen, Load more, Show more. Buttons that open another page ('Weiter', 'Next') belong in next_pattern with pages."},
            "next_pattern": {"type": "string", "description": "Texts of the next-page control for pages, separated by |, e.g. 'Nächste Seite|Vorwärts'. Default: Weiter, Nächste, Next, Vor, ›, », >, →; rel=next, aria-labels and page numbers are tried as well."},
            "scroll": {"type": "integer", "description": "Scroll to the bottom N times, for infinite scroll."},
            "fill": {"type": "object", "additionalProperties": {"type": "string"}, "description": "CSS selector → text to type before reading, e.g. {\"input[name=plz]\": \"74821\"}."},
            "select": {"type": "object", "additionalProperties": {"type": "string"}, "description": "CSS selector of a <select> → option label or value."},
            "submit": {"type": ["boolean", "string"], "description": "After fill/select: true presses Enter, a selector clicks that button."},
            "click_rows": {"type": "string", "description": "CSS selector of list rows that open by click instead of a link (e.g. 'td.bite-jobs-list--title'); each is opened and its target URL reported."},
            "wait_for": {"type": "string", "description": "CSS selector or text=… to wait for before extracting."},
            "iframes": {"type": "string", "enum": ["separate", "inline", "off"], "description": "separate (default): each iframe as its own section; inline: merged into the page."},
            "keep_nav": {"type": "boolean", "description": "full mode: keep nav/header/footer and link-heavy sidebars (sidebars with real text are kept anyway)."},
            "images": {"type": "boolean", "description": "Keep image links (dropped by default)."},
        }},
    },
    {
        "name": "render_pages",
        "description": "Render several pages at once (shared browser, up to 4 in parallel) with the same options, e.g. the job lists of ten municipalities filtered with grep='m/w/d'. One section per URL; failures are reported per URL and do not stop the others.",
        "inputSchema": {"type": "object", "required": ["urls"], "properties": {"urls": {"type": "array", "items": {"type": "string"}, "maxItems": 30}, **RENDER_PROPS}},
    },
    {
        "name": "find_careers",
        "description": "Given an employer's website (or its careers page), find the careers/job pages and identify the applicant tracking system (ATS) behind them: Personio, Workday, SmartRecruiters, Recruitee, d.vinci, softgarden, Lever, Greenhouse, SuccessFactors, Eightfold, dvvbw, mein-check-in, guidecom (with adapters), plus Phenom, b-ite, umantis, rexx, Avature, interamt and others (named, no adapter). Renders the pages, so JavaScript embeds are found. Use before guessing URLs like /karriere.",
        "inputSchema": {"type": "object", "required": ["url"], "properties": {"url": {"type": "string", "description": "Home page or careers page; a bare domain works."}}},
    },
    {
        "name": "ats_jobs",
        "description": "List every opening straight from the ATS, without a browser: title | location | date | URL. Pass any URL of the careers site or ATS (job ad, list, embed) and the system and tenant are detected; or pass ats plus tenant/site/base explicitly. query/location/exclude are case-insensitive regexes over title and location. Far more complete than reading the careers page, and paginated internally.",
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Careers page, job ad or ATS URL, e.g. https://kaco-new-energy-gmbh.jobs.personio.de or https://www.mein-check-in.de/wuerzburg."},
            "ats": {"type": "string", "enum": sorted(ats.ADAPTERS), "description": "Explicit system, if no URL is given."},
            "company": {"type": "string", "description": "Recruitee numeric company id (from a careers widget)."},
            "tenant": {"type": "string", "description": "Personio/Recruitee/d.vinci/softgarden/Lever/Greenhouse slug, SmartRecruiters company id, Workday tenant, mein-check-in or guidecom org."},
            "tld": {"type": "string", "enum": ["de", "com"], "description": "Personio domain (jobs.personio.de or .com)."},
            "site": {"type": "string", "description": "Workday career site name (with wd, e.g. wd3)."},
            "wd": {"type": "string", "description": "Workday data centre, e.g. wd1, wd3, wd5."},
            "base": {"type": "string", "description": "Origin for custom-domain systems (SuccessFactors, Eightfold, dvvbw), e.g. https://sma.jobs."},
            "domain": {"type": "string", "description": "Eightfold domain parameter, e.g. infineon.com."},
            "query": {"type": "string", "description": "Regex over title; also sent to systems with server-side search (Workday, SmartRecruiters, SuccessFactors, Eightfold)."},
            "location": {"type": "string", "description": "Regex over location, e.g. 'Mannheim|Heidelberg|Karlsruhe'. Workday also narrows server-side by its location facets (English names: 'Mannheim|Germany'), otherwise large employers exceed Workday's 2,000-hit cap."},
            "exclude": {"type": "string", "description": "Regex of titles to drop, e.g. 'Ausbildung|Praktikum|Werkstudent|Duales'."},
            "limit": {"type": "integer", "description": "Maximum jobs to list (default 200)."},
        }},
    },
]

# Lookbehinds keep "Wettbewerb", "Haltestellen" and "vorstellen" out.
CAREER_RE = re.compile(r"karriere|career|\bjobs?\b|/jobs?\b|(?<![a-zäöü])stellen|stellenangebot|ausschreibung|(?<![a-zäöü])bewerb|arbeiten[- ]bei|arbeitgeber|vacanc|job-?portal", re.I)
COMMON_PATHS = ["/karriere", "/jobs", "/stellenangebote", "/karriere/stellenangebote", "/career", "/careers", "/de/karriere", "/unternehmen/karriere", "/ueber-uns/karriere", "/rathaus/stellenangebote"]


async def page_snapshot(url, wait=6000):
    """Rendered HTML, iframe URLs and links of a page. Used for ATS detection, where embeds only exist after JavaScript has run."""
    b = await BROWSER.get()
    async with BROWSER.sem:
        ctx = await b.new_context(locale="de-DE", user_agent=UA, ignore_https_errors=True)
        try:
            page = await ctx.new_page()
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await settle(page, wait)
            html = await page.content()
            frames = " ".join(f.url for f in page.frames)
            links = await page.evaluate(LINKS)
            return {"status": resp.status if resp else 0, "url": page.url, "html": html + "\n<!-- frames: " + frames + " -->", "links": links}
        finally:
            await ctx.close()


def career_links(links, base):
    host = urllib.parse.urlsplit(base).netloc.split(".")[-2:]
    scored = {}
    for text, href in links:
        h = href.split("#")[0]
        if not CAREER_RE.search(text + " " + h) or re.search(r"\.(pdf|jpg|png)$|mailto:|facebook|linkedin|xing|instagram|kununu|stepstone|indeed", h, re.I):
            continue
        same = urllib.parse.urlsplit(h).netloc.split(".")[-2:] == host
        score = 2 * bool(CAREER_RE.search(text)) + bool(CAREER_RE.search(h)) + same
        if h not in scored or scored[h][0] < score:
            scored[h] = (score, text)
    return sorted(((s, t, h) for h, (s, t) in scored.items()), reverse=True)


def describe(found):
    lines = []
    for name, params, how in found:
        if name in ats.ADAPTERS:
            args = ", ".join(f"{k}={v!r}" for k, v in params.items())
            lines.append(f"- {name} ({how}): {args} → ats_jobs(ats={name!r}, {args})")
        else:
            hint = next((v for k, v in ats.NOTES.items() if k in name), "")
            lines.append(f"- {name} ({how}) — no adapter. {hint}".rstrip())
    return lines


async def find_careers(url):
    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")
    out, notes, found = [], [], []
    try:
        snap = await page_snapshot(url)
    except Exception as e:
        raise RuntimeError(f"could not load {url}: {str(e).splitlines()[0]}")
    found += ats.detect(snap["url"], snap["html"])
    cands = career_links(snap["links"], snap["url"])
    if not cands:
        for path in COMMON_PATHS:
            probe = ats.origin(snap["url"]) + path
            try:
                status, final, _ = await asyncio.to_thread(ats.http, probe)
            except ats.AtsError:
                continue
            if status < 400:
                cands.append((1, f"(probed {path})", final))
        notes.append("no careers link on the page; probed common paths")
    visited = {snap["url"]}
    for _, text, href in cands[:3]:
        if any(f[0] in ats.ADAPTERS for f in found):
            break
        if href in visited:
            continue
        visited.add(href)
        try:
            sub = await page_snapshot(href)
        except Exception as e:
            notes.append(f"{href}: {str(e).splitlines()[0]}")
            continue
        found += [f for f in ats.detect(sub["url"], sub["html"]) if f not in found]
        # One level deeper: a "Karriere" landing page usually links to "Stellenangebote".
        for _, t2, h2 in career_links(sub["links"], sub["url"])[:2]:
            if h2 not in visited and not any(f[0] in ats.ADAPTERS for f in found):
                visited.add(h2)
                try:
                    deep = await page_snapshot(h2)
                    found += [f for f in ats.detect(deep["url"], deep["html"]) if f not in found]
                except Exception:
                    pass
    out.append(f"Site: {snap['url']}")
    out.append("\nCareers pages (best first):")
    out += [f"- [{t or h}]({h})" for _, t, h in cands[:8]] or ["- none found"]
    out.append("\nApplicant tracking systems:")
    out += describe(found) or ["- none recognised; read the careers page with render_page (mode full, grep 'm/w/d')"]
    out += [f"Note: {n}" for n in notes]
    out.append(f"Note: checked {len(visited)} page(s): " + ", ".join(sorted(visited)))
    return "\n".join(out)


async def ats_jobs(a):
    found = []
    if a.get("ats"):
        params = {k: a[k] for k in ("tenant", "company", "tld", "site", "wd", "base", "domain") if a.get(k)}
        found = [(a["ats"], params, "explicit")]
    elif a.get("url"):
        url = a["url"] if a["url"].startswith("http") else "https://" + a["url"]
        found = [f for f in ats.detect(url) if f[0] in ats.ADAPTERS]
        if not found:
            snap = await page_snapshot(url)
            found = [f for f in ats.detect(snap["url"], snap["html"]) if f[0] in ats.ADAPTERS]
            if not found:
                others = describe(ats.detect(snap["url"], snap["html"]))
                raise RuntimeError("no supported ATS found on that page." + ("\n" + "\n".join(others) if others else " Try find_careers on the employer's home page, or render_page on the list."))
    else:
        raise RuntimeError("give url, or ats plus tenant/site/base")
    name, params, how = found[0]
    # Systems with a search box get the query server-side (it also matches the ad text, not just the title), and their answer is then not filtered by title again. Only plain words are sent; a regex is applied client-side instead.
    server_q = a.get("query", "") if name in ("workday", "smartrecruiters", "successfactors", "eightfold") and re.fullmatch(r"[\w äöüÄÖÜß-]+", a.get("query", "")) else ""
    jobs = await asyncio.to_thread(ats.fetch_jobs, name, params, server_q, a.get("location", "") if name in ("eightfold", "workday") else "")
    hits = ats.filter_jobs(jobs, "" if server_q else a.get("query", ""), a.get("location", ""), a.get("exclude", ""))
    limit = a.get("limit") or 200
    head = f"ATS: {name} ({', '.join(f'{k}={v}' for k, v in params.items())}; detected from {how}). {len(hits)} of {len(jobs)} jobs match" + (f"; showing {limit}" if len(hits) > limit else "") + "."
    if len(found) > 1:
        head += "\nAlso found: " + "; ".join(f"{n} {p}" for n, p, _ in found[1:])
    return head + "\n\n" + (ats.format_jobs(hits[:limit]) or "(no matching jobs)")


async def call_tool(name, a):
    if name == "render_page":
        opts = {k: v for k, v in a.items() if k != "url"}
        return format_markdown(await BROWSER.render(a["url"], **opts))
    if name == "render_pages":
        opts = {k: v for k, v in a.items() if k != "urls"}

        async def one(u):
            try:
                return f"===== {u}\n" + format_markdown(await BROWSER.render(u, **opts))
            except Exception as e:
                return f"===== {u}\nError: {str(e).splitlines()[0]}"
        return "\n\n".join(await asyncio.gather(*(one(u) for u in a["urls"])))
    if name == "find_careers":
        return await find_careers(a["url"])
    if name == "ats_jobs":
        return await ats_jobs(a)
    raise RuntimeError(f"unknown tool {name}")


async def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    ok = lambda result: {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        return ok({"protocolVersion": (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "web-renderer", "version": "1.0.0"}})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            text = await call_tool(p.get("name"), p.get("arguments") or {})
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as e:
            # A tool result rather than a protocol error, so the model sees the reason and can correct itself.
            return ok({"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
    if method == "ping":
        return ok({})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


async def serve():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    write_lock = asyncio.Lock()
    tasks = set()

    async def respond(msg):
        reply = await handle(msg)
        if reply is not None:
            async with write_lock:
                sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                sys.stdout.flush()

    # Each request runs as its own task, so a slow render does not hold up the next call; replies go out in completion order, matched by id.
    while line := await reader.readline():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = asyncio.create_task(respond(msg))
        tasks.add(t)
        t.add_done_callback(tasks.discard)
    if tasks:
        await asyncio.gather(*tasks)
    await BROWSER.close()
