"""Post-processing of the extracted Markdown (link tidying, grep, parts) and the text format of a render result."""

import re

LONG_URL = 100

LINK_RE = re.compile(r"(?<!!)\[([^\[\]]*)\]\(([^)\s]*)((?:\s+\"[^\"]*\")?)\)")


def short_url(url):
    """A long URL cut so that it stays recognisable: without its query and fragment ('https://geizhals.de/x.html?…'), and if that alone is still long, down to the first path segment ('https://geizhals.de/redir/…'). A URL whose length is all in one path segment stays whole, since any cut would break it. Tracking and redirect links (Geizhals offers, ad networks) run to 200+ characters each and made up 62 % of a Geizhals product page."""
    m = re.match(r"(https?://[^/?#]+)(/[^?#]*)?([?#].*)?$", url)
    if not m:
        return url
    origin, path, rest = m.group(1), m.group(2) or "", m.group(3)
    if len(origin + path) <= LONG_URL:
        return origin + path + ("?…" if rest else "")
    segments = path.strip("/").split("/")
    if len(segments) > 1:
        return f"{origin}/{segments[0]}/…"
    return origin + path + ("?…" if rest else "")


def compact_links(md, full_urls, notes):
    """Tidy Markdown links so they cost fewer tokens without losing a target:
    - line breaks inside link texts collapse, links without text and titles that repeat the text go;
    - jump marks within the page (#offerlist) and repeats of a target already linked above (ignoring its #fragment) become plain text: a Geizhals search result links the same product page four times (title, rating, offers, price);
    - unless full_urls is set, targets longer than LONG_URL characters are cut as in short_url."""
    shortened = repeated = 0
    seen = set()

    def one(m):
        nonlocal shortened, repeated
        text, url, title = re.sub(r"\s+", " ", m.group(1)).strip(), m.group(2), m.group(3)
        if not text:
            return ""
        base = url.split("#")[0]
        if not base or base in seen:
            repeated += bool(base)
            return text
        seen.add(base)
        if title.strip().strip('"').strip() in text:
            title = ""
        if not full_urls and len(url) > LONG_URL and short_url(url) != url:
            shortened += 1
            url, title = short_url(url), ""
        return f"[{text}]({url}{title})"
    md = LINK_RE.sub(one, md)
    if shortened:
        notes.append(f"shortened {shortened} link target(s) longer than {LONG_URL} characters (parameters or deeper path cut to …); full_urls keeps them, links lists them")
    if repeated:
        notes.append(f"{repeated} repeated link(s) to a target already linked above shown as plain text")
    return md


def clean_markdown(md, images, full_urls, notes):
    if not images:
        md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    md = re.sub(r"(?is)<(video|audio|picture|source)\b[^>]*>(.*?</\1>)?", "", md)
    md = compact_links(md, full_urls, notes)
    # List markers and headings left without text once images and links are gone, e.g. Geizhals' empty variant lists and Fab's gallery numbering.
    md = re.sub(r"(?m)^[ \t]*(?:[-*+]|\d+\.|#{1,6})[ \t]*$", "", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md)


def parts(body, size):
    """Split body into chunks of at most size characters, cut at a paragraph (else line) break in the second half of each chunk, so no part ends mid-sentence. Returns (start, end) pairs."""
    if size <= 0 or len(body) <= size:
        return [(0, len(body))]
    out, pos = [], 0
    while pos < len(body):
        end = min(len(body), pos + size)
        if end < len(body):
            half = pos + size // 2
            cut = body.rfind("\n\n", half, end)
            if cut < 0:
                cut = body.rfind("\n", half, end)
            if cut > 0:
                end = cut
        out.append((pos, end))
        pos = end
        while pos < len(body) and body[pos] == "\n":
            pos += 1
    return out


def filter_lines(md, grep, exclude, context):
    """Keep lines matching grep (with `context` lines around each), drop lines matching exclude. Headings that precede a kept line are kept too, so the result still says which section a hit came from."""
    lines = md.split("\n")
    ex = re.compile(exclude, re.I) if exclude else None
    if ex:
        lines = [l for l in lines if not ex.search(l)]
    if not grep:
        return "\n".join(lines)
    rx = re.compile(grep, re.I)
    keep = set()
    for i, l in enumerate(lines):
        # Section markers of further pages and iframes stay, so hits can still be told apart by source.
        if l.startswith(("## Seite ", "## iframe ")):
            keep.add(i)
        elif rx.search(l):
            keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
            for j in range(i - 1, -1, -1):
                if lines[j].startswith("#"):
                    keep.add(j)
                    break
    out, prev = [], -2
    for i in sorted(keep):
        if i != prev + 1 and out:
            out.append("…")
        out.append(lines[i])
        prev = i
    return "\n".join(out)


def format_markdown(r):
    out = [f"Title: {r['title']}", f"URL: {r['url']}", f"Mode: {r['mode']}"]
    out += [f"Note: {n}" for n in r["notes"]]
    out += ["", r["content"]]
    if r.get("parts") and r["parts"] > 1:
        nxt = f"next: part={r['part'] + 1}" if r["part"] < r["parts"] else "last part"
        out.append(f"\n[part {r['part']}/{r['parts']}: characters {r['start']}-{r['end']} of {r['total_chars']}; {nxt}]")
    elif r["end"] < r["total_chars"]:
        out.append(f"\n[truncated: characters {r['start']}-{r['end']} of {r['total_chars']}; continue with start={r['end']}]")
    if r["rows"]:
        out.append(f"\n## Rows ({len(r['rows'])})\n")
        out += [f"- {t} → {u}" for t, u in r["rows"]]
    if r["links"]:
        out.append(f"\n## Links ({len(r['links'])})\n")
        out += [f"- [{t or h}]({h})" for t, h in r["links"]]
    return "\n".join(out)
