"""Web renderer: reads web pages that plain fetchers cannot (JavaScript lists, load-more buttons, pagination, forms, rows without links, iframes, web components, PDFs) and returns them as clean Markdown, plus job lists straight from applicant tracking systems.

Modules, in the order a render passes through them:
- __main__: entry point; `python -m web_renderer mcp` serves MCP, anything else is the command line
- environment: re-runs under nixpkgs' Python with Playwright when started without it
- cli, mcp_server: the two front ends
- render: one URL end to end; Renderer keeps one Chromium per process
- options: defaults and normalisation of the render options
- headless: loading a page in headless Chromium (Playwright), PDFs, pagination, iframes
- agent_vivaldi: the same in the agent Vivaldi over DevTools, for pages behind a bot check
- page_actions: clicking, forms, consent, scrolling, iframe discovery
- extraction: Defuddle, Readability and whole-page extraction and the choice between them
- markdown: tidying, grep, parts and the text format of a result
- ats: job lists from applicant tracking system APIs (stdlib only)
"""
