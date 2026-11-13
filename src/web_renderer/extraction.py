"""Turning a rendered frame into Markdown: Defuddle, Readability and a whole-page mode run in the page, and pick() chooses the result."""

import os
import re
import urllib.request
from pathlib import Path

JS_LIBS = {
    "defuddle": "https://cdn.jsdelivr.net/npm/defuddle@0.19.4/dist/index.full.js",
    "readability": "https://cdn.jsdelivr.net/npm/@mozilla/readability@0.6.0/Readability.js",
}
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "web-renderer"
AUTO_FULL_RATIO = 1.3
AUTO_FRAGMENT_RATIO = 3
AUTO_SMALL_PAGE = 3000
NOISE_SELECTOR_RE = r"cookie|consent|gdpr|usercentrics|borlabs|klaro|cmp[-_]|onetrust|ccm-|skip-?link"


def js_lib(name):
    path = CACHE / (JS_LIBS[name].split("/npm/")[1].replace("/", "_"))
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(JS_LIBS[name], timeout=30) as r:
            path.write_bytes(r.read())
    return path.read_text()


def extractor_scripts():
    """JavaScript functions that install Defuddle and Readability as globals of the page. Evaluated via the DevTools protocol, so the page's Content-Security-Policy cannot block them.

    Defuddle ships as a UMD bundle: on pages with an AMD loader (SAP SuccessFactors/UI5, Wix, RequireJS) it registers itself through `define()` instead of setting `self.Defuddle`, and extraction then failed with "Defuddle is not defined". Local `define`/`module`/`exports` shadow the page's globals so the bundle always takes the plain-global branch."""
    return [
        "() => { var define, module, exports;\n" + js_lib("defuddle") + "\n}",
        "() => { var define, module, exports;\n" + js_lib("readability") + "\n window.Readability = Readability; }",
    ]


async def load_libs(frame):
    for script in extractor_scripts():
        await frame.evaluate(script)


MARK_HIDDEN = """() => {
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      const s = getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden' || el.getAttribute('aria-hidden') === 'true') el.setAttribute('data-rp-hidden', '');
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
}"""

# Web components (e.g. Jobcluster's <jcd-job-portal>) keep their content in a shadow root, which cloneNode, Defuddle and Readability never see. Copy each open shadow root into its host as ordinary children, innermost first.
FLATTEN_SHADOW = """() => {
  let n = 0;
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (!el.shadowRoot) continue;
      walk(el.shadowRoot);
      const d = document.createElement('div');
      d.setAttribute('data-shadow-root', el.tagName.toLowerCase());
      d.innerHTML = el.shadowRoot.innerHTML;
      el.appendChild(d);
      n++;
    }
  };
  walk(document);
  return n;
}"""

EXTRACT = """(args) => {
  const [mode, keepNav, noiseRe] = args;
  const url = location.href;
  const out = {};
  const md = (html) => Defuddle.createMarkdownContent(html, url);
  const re = new RegExp(noiseRe, 'i');
  // Every extractor gets a copy without hidden elements, media and consent banners; otherwise Readability happily returns a cookie dialog as the "article".
  const cleanClone = () => {
    const doc = document.cloneNode(true);
    doc.querySelectorAll('[data-rp-hidden],[role=dialog],[role=alertdialog],video,audio,picture source,canvas').forEach(e => e.remove());
    doc.querySelectorAll('[id],[class]').forEach(e => { if (re.test((e.id || '') + ' ' + (typeof e.className === 'string' ? e.className : ''))) e.remove(); });
    return doc;
  };
  if (mode === 'defuddle' || mode === 'auto') {
    try {
      const r = new Defuddle(cleanClone(), { url, markdown: true }).parse();
      out.defuddle = { content: r.content || '', title: r.title, author: r.author, published: r.published };
    } catch (e) { out.defuddle = { content: '', error: String(e) }; }
  }
  if (mode === 'readability' || mode === 'auto') {
    try {
      const r = new Readability(cleanClone()).parse();
      out.readability = { content: r && r.content ? md(r.content) : '', title: r && r.title };
    } catch (e) { out.readability = { content: '', error: String(e) }; }
  }
  if (mode === 'full' || mode === 'auto') {
    const doc = cleanClone();
    const drop = 'script,style,noscript,svg,template,iframe,link,meta' + (keepNav ? '' : ',nav,header,footer,[role=navigation],[role=banner],[role=contentinfo]');
    doc.querySelectorAll(drop).forEach(e => e.remove());
    // Sidebars hold real data as often as noise: Fab puts price, license and publish date in an <aside>. Drop one only when it is mostly links (related products, tag clouds) or nearly empty.
    let asides = 0;
    if (!keepNav) {
      const linkDense = (e) => {
        const text = (e.textContent || '').replace(/\\s+/g, '').length;
        const linked = [...e.querySelectorAll('a')].reduce((n, a) => n + (a.textContent || '').replace(/\\s+/g, '').length, 0);
        return text < 30 || linked > 0.6 * text;
      };
      doc.querySelectorAll('aside,[role=complementary]').forEach(e => { if (linkDense(e)) { e.remove(); asides++; } });
    }
    try { out.full = { content: doc.body ? md(doc.body.innerHTML) : '', asides }; }
    catch (e) { out.full = { content: doc.body ? doc.body.innerText : '', error: String(e), asides }; }
  }
  if (mode === 'html') out.html = { content: document.documentElement.outerHTML };
  return out;
}"""

LINKS = """() => [...document.querySelectorAll('a[href]')].map(a => [(a.innerText || a.title || a.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim(), a.href]).filter(([t, h]) => h.startsWith('http'))"""


def text_weight(md):
    """Characters of real text, ignoring link targets and Markdown syntax, so a block of links does not outweigh a paragraph."""
    md = re.sub(r"\]\([^)]*\)", "]", md)
    return len(re.sub(r"[#*_>\[\]|`-]|\s+", "", md))


HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")


def heading_key(line):
    """Heading text without Markdown links and punctuation, so '## [7 Produkteigenschaften](#data)' matches across extractors."""
    return re.sub(r"\]\([^)]*\)|[^\w]+", "", line).lower()


def emptied_sections(extracted, full):
    """Headings the extractor kept but left empty while the whole page has text under them. Readability did this to the spec table and document list of a Geizhals product page (2026-11-13), which the length ratio alone did not catch because the page also carries a long sidebar."""
    def sections(md):
        out, key, size = {}, None, 0
        for line in md.split("\n"):
            m = HEADING_RE.match(line)
            if m:
                if key is not None:
                    out.setdefault(key, size)
                key, size = heading_key(m.group(1)), 0
            elif key is not None:
                size += text_weight(line)
        if key is not None:
            out.setdefault(key, size)
        return out
    mine, whole = sections(extracted), sections(full)
    return [k for k, size in mine.items() if k and size < 20 and whole.get(k, 0) >= 60]


def pick(res, mode, notes):
    """Choose the extraction result to return and say why."""
    if mode != "auto":
        chosen = mode
    else:
        weights = {k: text_weight(res[k]["content"]) for k in ("defuddle", "readability")}
        chosen = max(weights, key=weights.get)
        full = text_weight(res["full"]["content"])
        # If the whole page is barely longer than the best extraction, the page is nearly all content and the extractor only risks dropping items (Defuddle lost 3 of 21 jobs on a Jobcluster list). Much longer means the extractor cut real boilerplate, so keep its result, unless it emptied sections the page has text in.
        emptied = emptied_sections(res[chosen]["content"], res["full"]["content"])
        # Under a third is not boilerplate removal but a wrong guess at the "article": on the Geizhals search (2026-11-13) Readability returned only the price disclaimer below a 30-item product grid.
        fragment = full > AUTO_FRAGMENT_RATIO * weights[chosen]
        # A short page costs few tokens even whole, and extractors cut its sidebars, where shops keep price and license (Fab, 2026-11-13).
        small = full <= AUTO_SMALL_PAGE
        if full >= weights[chosen] and (weights[chosen] < 200 or small or full <= AUTO_FULL_RATIO * weights[chosen] or emptied or fragment):
            if emptied:
                notes.append(f"{chosen} left {len(emptied)} section(s) empty that have text on the page; using full")
            elif fragment:
                notes.append(f"{chosen} kept less than 1/{AUTO_FRAGMENT_RATIO} of the page text; using full")
            chosen = "full"
        notes.append("auto picked " + chosen + " (text chars: " + ", ".join(f"{k} {v}" for k, v in weights.items()) + f", full {full}); override with mode if this looks wrong")
    if chosen == "full" and res["full"].get("asides"):
        notes.append(f"dropped {res['full']['asides']} sidebar(s) that were mostly links; keep_nav keeps them")
    if res[chosen].get("error"):
        notes.append(f"{chosen} failed: {res[chosen]['error']}")
    return chosen, res[chosen]["content"].strip()


async def extract_frame(frame, o, notes):
    """Extract one frame as a document of its own: its own CSS decides what is hidden, its own shadow roots are flattened, and the extractor is chosen for it alone. Falls back to the frame's plain innerText if the extractors cannot even be injected."""
    try:
        if not o["no_details"]:
            await frame.evaluate("() => document.querySelectorAll('details:not([open])').forEach(d => d.open = true)")
        await frame.evaluate(MARK_HIDDEN)
        if not o["no_shadow"]:
            shadows = await frame.evaluate(FLATTEN_SHADOW)
            if shadows:
                notes.append(f"flattened {shadows} shadow root(s)")
        await load_libs(frame)
        res = await frame.evaluate(EXTRACT, [o["mode"], o["keep_nav"], NOISE_SELECTOR_RE])
        chosen, body = pick(res, o["mode"], notes)
    except Exception as e:
        notes.append(f"extractors failed ({str(e).splitlines()[0]}); returning the page's plain text")
        chosen, body = "innertext", (await frame.evaluate("() => document.body ? document.body.innerText : ''")).strip()
    links = await frame.evaluate(LINKS) if (o["links"] or o["links_only"]) else []
    return chosen, body, links
