# web-renderer: reads web pages in headless Chromium, and in the agent Vivaldi after a bot check. `web-renderer URL` on the command line, `web-renderer mcp` as stdio MCP server.
{
  lib,
  writeShellApplication,
  python3,
  playwright-driver,
  agent-browser,
  # DevTools port of the agent Vivaldi; must match agent-browser's.
  port ? 9333,
}:

let
  # src/ with only the Python files of the package web_renderer, to go on PYTHONPATH.
  pythonPath = lib.fileset.toSource {
    root = ./src;
    fileset = lib.fileset.fileFilter (file: file.hasExt "py") ./src/web_renderer;
  };

  # Playwright and the Chromium build it expects come from the same nixpkgs, so their versions match.
  python = python3.withPackages (ps: [ ps.playwright ps.pypdf ps.websockets ]);
in
writeShellApplication {
  name = "web-renderer";
  text = ''
    export PLAYWRIGHT_BROWSERS_PATH=${playwright-driver.browsers}
    export AGENT_BROWSER=${lib.getExe agent-browser}
    export AGENT_BROWSER_PORT="''${AGENT_BROWSER_PORT:-${toString port}}"
    export PYTHONPATH=${pythonPath}
    exec ${python}/bin/python3 -P -m web_renderer "$@"
  '';
  meta = {
    description = "Read web pages that plain fetchers cannot, as Markdown; command line and MCP server";
    license = lib.licenses.mit;
    mainProgram = "web-renderer";
    platforms = lib.platforms.linux;
  };
}
