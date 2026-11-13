# agent-browser: starts the agent Vivaldi with its own profile and a DevTools port on 127.0.0.1 (see agent-browser.sh).
{
  lib,
  writeShellApplication,
  curl,
  util-linux,
  coreutils,
  gnused,
  # Vivaldi to start. Null starts the `vivaldi` found in PATH, so no second Vivaldi is installed. Not named `vivaldi`, or callPackage would fill in nixpkgs' Vivaldi.
  vivaldiPackage ? null,
  port ? 9333,
}:

writeShellApplication {
  name = "agent-browser";
  runtimeInputs = [ curl util-linux coreutils gnused ];
  text = ''
    export AGENT_BROWSER_PORT="''${AGENT_BROWSER_PORT:-${toString port}}"
  ''
  + lib.optionalString (vivaldiPackage != null) ''
    export AGENT_BROWSER_VIVALDI="''${AGENT_BROWSER_VIVALDI:-${lib.getExe vivaldiPackage}}"
  ''
  + builtins.readFile ./agent-browser.sh;
  meta = {
    description = "Start the agent Vivaldi with its own profile and a DevTools port";
    license = lib.licenses.mit;
    mainProgram = "agent-browser";
    platforms = lib.platforms.linux;
  };
}
