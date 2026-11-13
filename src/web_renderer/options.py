"""Render options: defaults, button-text patterns and the normalisation every render starts with."""

# Button and link texts that load more items in place (the page grows, the URL stays). Anchored so that "Weitere Informationen" or "Mehr erfahren" do not match. Override per call with expand_pattern; texts that move to another page ("Weiter", "Next") belong in NEXT_RE instead.
EXPAND_RE = r"^\s*(\+\s*)?(mehr laden|mehr anzeigen|weitere laden|weitere anzeigen|weitere ergebnisse|weitere stellen(angebote)?( anzeigen| laden)?|alle anzeigen|alle stellen( anzeigen)?|load more|show more|more results|see more|view more)\s*(\(\d+\))?\s*$"
# Texts of the control that moves to the next page of a paginated list. Override per call with next_pattern.
NEXT_RE = r"^\s*(weiter|nächste( seite)?|next( page)?|vor|›|»|>|→)\s*$"


DEFAULTS = dict(mode="auto", max_length=20000, start=0, part=None, full_urls=False, browser="auto", expand=10, expand_pattern=EXPAND_RE, scroll=0, pages=0, next_pattern=NEXT_RE, iframes="separate", max_iframes=5, iframe_depth=3, no_shadow=False, accept_cookies=False, keep_nav=False, no_details=False, wait_for=None, wait=8000, timeout=45000, links=None, links_only=False, grep=None, exclude=None, context=0, images=False, load_media=False, screenshot=None, click_rows=None, max_rows=30, fill=None, select=None, submit=False)


def button_pattern(texts):
    """Turn plain button texts ('Weitere Stellen|Mehr Treffer') into an anchored, case-insensitive regex, so 'Weiter' does not also match 'Weitere Informationen'. A pattern that already starts with ^ is used as given."""
    if texts.lstrip().startswith("^"):
        return texts
    return r"^\s*(\+\s*)?(" + texts + r")\s*(\(\d+\))?\s*$"


def normalise(o):
    o = {**DEFAULTS, **{k: v for k, v in o.items() if v is not None}}
    notes = []
    o["expand_pattern"] = button_pattern(o["expand_pattern"])
    o["next_pattern"] = button_pattern(o["next_pattern"])
    # A grep runs over the text anyway, so the whole page is the right input: an extractor may have dropped exactly the matching list items.
    if o["grep"] and o["mode"] == "auto":
        o["mode"] = "full"
        notes.append("grep: searched the whole page (mode full)")
    # 35 calls in the 2026-10-07 job search passed seconds (`--wait 5`) and so waited 5 ms. Nobody means a 5 ms network-idle wait.
    for key in ("wait", "timeout"):
        if 0 < o[key] < 100:
            notes.append(f"{key}={o[key]} read as seconds ({o[key] * 1000} ms)")
            o[key] = o[key] * 1000
    return o, notes
