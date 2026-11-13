"""Command line of the web renderer: render web pages in headless Chromium (the agent Vivaldi after a bot check) and print them as clean Markdown.

Typical calls:
  web-renderer URL                                    auto mode: expands lists, reads iframes, picks the better extractor
  web-renderer URL --grep 'm/w/d' --exclude 'Ausbildung|Praktikum'    only matching lines
  web-renderer URL --links-only --links 'stelle|job'  just the links, to find sub-pages
  web-renderer URL --pages 5                          follow numbered or "Weiter" pagination
  web-renderer URL --fill '#plz=74821' --submit       fill a search form first
  web-renderer --urls list.txt --parallel 4 --grep 'm/w/d'   many pages at once, one browser
  web-renderer --python my.py ARGS                    run your own Playwright script in the same environment

Run with --help for every option.
"""

import argparse
import asyncio
import json
import os
import sys

from .markdown import format_markdown
from .options import DEFAULTS, EXPAND_RE, NEXT_RE
from .render import Renderer


def pairs(items):
    out = {}
    for item in items or []:
        sel, _, value = item.partition("=")
        out[sel] = value
    return out


def main():
    ap = argparse.ArgumentParser(prog="web-renderer", description=__doc__.split("\n\n")[0], epilog="web-renderer mcp runs the stdio MCP server (render_page, render_pages, find_careers, ats_jobs) instead.\n\nExtraction modes: auto runs Defuddle, Readability and full side by side and keeps the extractor's result unless the page holds at most 3,000 characters, the extractor kept less than a third of the text or left a section empty; then it takes full; defuddle = Obsidian Web Clipper's engine, good on lists and portals; readability = Firefox reader view, good on single articles and job ads; full = whole page minus navigation, banners and hidden elements; html = rendered HTML for debugging.\n\niframes: separate (default) extracts every visible iframe as a page of its own and prints it as a section after the main page; inline copies iframe contents into the parent page; off ignores them.\n\nTimes are milliseconds; values below 100 are read as seconds.", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?")
    ap.add_argument("--urls", metavar="FILE", help="render every URL in FILE (one per line; a leading name before the URL is allowed, '#' starts a comment), '-' for stdin")
    ap.add_argument("--parallel", type=int, default=4, help="pages rendered at once with --urls (default 4)")
    ap.add_argument("--python", metavar="SCRIPT", help="run SCRIPT with this tool's Python (Playwright, pypdf, PLAYWRIGHT_BROWSERS_PATH set); remaining arguments go to the script")
    ap.add_argument("--mode", choices=["auto", "defuddle", "readability", "full", "html"], default="auto")
    ap.add_argument("--max-length", type=int, default=20000, help="characters to print (default 20000, 0 = all)")
    ap.add_argument("--part", type=int, help="print part N of a page longer than --max-length (cut at paragraph breaks)")
    ap.add_argument("--start", type=int, default=0, help="character offset, an alternative to --part")
    ap.add_argument("--full-urls", action="store_true", help="keep link targets longer than 100 characters (shortened to host/path/… by default)")
    ap.add_argument("--browser", choices=["auto", "headless", "agent"], default="auto", help="auto: headless, and the agent Vivaldi (agent-browser) after a bot check; agent: always Vivaldi")
    ap.add_argument("--grep", metavar="REGEX", help="print only lines matching REGEX (case-insensitive), with the heading above each")
    ap.add_argument("--exclude", metavar="REGEX", help="drop lines matching REGEX, e.g. 'Ausbildung|Praktikum|Werkstudent'")
    ap.add_argument("--context", type=int, default=0, help="lines of context around --grep hits")
    ap.add_argument("--links", nargs="?", const=".", metavar="REGEX", help="also print the page's links, optionally only those whose text or URL matches REGEX")
    ap.add_argument("--links-only", action="store_true", help="print only the links (implies --links)")
    ap.add_argument("--expand", type=int, default=10, help="click load-more buttons up to N times per frame, stopping when a click adds no text (default 10, 0 = off)")
    ap.add_argument("--expand-pattern", default=EXPAND_RE, help="load-more button texts separated by |, e.g. 'Weitere Stellen|Mehr Treffer' (whole text, case-insensitive; a value starting with ^ is used as a raw regex)")
    ap.add_argument("--pages", type=int, default=0, help="follow pagination up to page N (a 'Weiter'/'›' control, else the button labelled with the next number)")
    ap.add_argument("--next-pattern", default=NEXT_RE, help="next-page control texts separated by |, e.g. 'Nächste Seite|Vorwärts' (default: Weiter, Nächste, Next, ›, », →)")
    ap.add_argument("--scroll", type=int, default=0, help="scroll to the bottom N times, for infinite-scroll pages")
    ap.add_argument("--fill", action="append", metavar="SELECTOR=TEXT", help="type TEXT into the field at SELECTOR before reading (repeatable)")
    ap.add_argument("--select", action="append", metavar="SELECTOR=OPTION", help="choose OPTION (label or value) in the <select> at SELECTOR (repeatable)")
    ap.add_argument("--submit", nargs="?", const=True, default=False, metavar="SELECTOR", help="after --fill/--select press Enter, or click SELECTOR")
    ap.add_argument("--click-rows", metavar="SELECTOR", help="click each element matching SELECTOR and report where it leads (rows without links)")
    ap.add_argument("--max-rows", type=int, default=30)
    ap.add_argument("--iframes", choices=["separate", "inline", "off"], default="separate")
    ap.add_argument("--max-iframes", type=int, default=5)
    ap.add_argument("--iframe-depth", type=int, default=3)
    ap.add_argument("--no-shadow", action="store_true", help="do not copy web-component shadow DOM into the page before extraction")
    ap.add_argument("--accept-cookies", action="store_true", help="click a consent 'Akzeptieren'/'Accept' button first (any frame)")
    ap.add_argument("--keep-nav", action="store_true", help="full mode: keep nav/header/footer and link-heavy sidebars")
    ap.add_argument("--no-details", action="store_true", help="do not open collapsed <details> sections")
    ap.add_argument("--images", action="store_true", help="keep Markdown image links (dropped by default)")
    ap.add_argument("--wait-for", help="CSS selector or text=... to wait for before extracting")
    ap.add_argument("--wait", type=int, default=8000, help="ms to wait for the network to go idle after load and after each click (default 8000)")
    ap.add_argument("--timeout", type=int, default=45000, help="navigation timeout in ms (default 45000)")
    ap.add_argument("--load-media", action="store_true", help="load images, fonts and media (blocked by default for speed)")
    ap.add_argument("--screenshot", metavar="PATH", help="also save a full-page PNG")
    ap.add_argument("--json", action="store_true", help="print everything as JSON")
    args, rest = ap.parse_known_args()

    if args.python:
        os.execv(sys.executable, [sys.executable, args.python, *rest])
    if rest:
        ap.error(f"unrecognized arguments: {' '.join(rest)}")
    if not args.url and not args.urls:
        ap.error("give a URL or --urls FILE")

    opts = {k: getattr(args, k) for k in DEFAULTS if hasattr(args, k)}
    opts.update(fill=pairs(args.fill), select=pairs(args.select))
    urls = [args.url] if args.url else read_urls(args.urls)
    sys.exit(asyncio.run(run(urls, opts, args)))


def read_urls(path):
    lines = sys.stdin if path == "-" else open(path, encoding="utf-8")
    out = []
    for line in lines:
        line = line.split("#", 1)[0].strip() if not line.lstrip().startswith("http") else line.strip()
        url = next((w for w in line.split() if w.startswith("http")), None)
        if url:
            out.append(url)
    return out


async def run(urls, opts, args):
    b = Renderer(concurrency=max(1, args.parallel))
    failed = 0

    async def one(url):
        try:
            return url, await b.render(url, **opts), None
        except Exception as e:
            return url, None, str(e).splitlines()[0]
    try:
        results = await asyncio.gather(*(one(u) for u in urls))
    finally:
        await b.close()
    for n, (url, r, err) in enumerate(results):
        if err:
            failed += 1
        if args.json:
            print(json.dumps(r or {"url": url, "error": err}, ensure_ascii=False, indent=1))
            continue
        if len(urls) > 1:
            print(("\n" if n else "") + f"===== {url}")
        print(f"web-renderer: {err}" if err else format_markdown(r))
    return 1 if failed == len(urls) else 0
