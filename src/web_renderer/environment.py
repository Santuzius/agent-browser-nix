"""Python environment of the web renderer: re-run under nixpkgs' Python with Playwright, its Chromium and pypdf when started without them (straight from the checkout instead of the installed `web-renderer` command)."""

import os
import subprocess
import sys

NIX_ENV = 'let p = import (builtins.getFlake "nixpkgs") {}; in [ (p.python3.withPackages (ps: [ ps.playwright ps.pypdf ps.websockets ])) p.playwright-driver.browsers ]'


def ensure_playwright():
    """Re-exec `python -m web_renderer` with the same arguments under nixpkgs' Python with Playwright, pypdf and websockets, unless already there (the installed command is)."""
    try:
        import playwright  # noqa: F401
        if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            return
    except ImportError:
        pass
    if os.environ.get("WEB_RENDERER_BOOTSTRAPPED"):
        sys.exit("web-renderer: Playwright still missing after bootstrapping through nix")
    paths = subprocess.run(["nix", "build", "--no-link", "--print-out-paths", "--impure", "--expr", NIX_ENV], capture_output=True, text=True)
    if paths.returncode:
        sys.exit("web-renderer: could not build Playwright from nixpkgs:\n" + paths.stderr[-2000:])
    pyenv = next(p for p in paths.stdout.split() if "python3" in p)
    browsers = next(p for p in paths.stdout.split() if "playwright-browsers" in p)
    package_parent = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=browsers, WEB_RENDERER_BOOTSTRAPPED="1", PYTHONPATH=package_parent)
    os.execve(f"{pyenv}/bin/python3", [f"{pyenv}/bin/python3", "-m", "web_renderer", *sys.argv[1:]], env)
