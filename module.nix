# Home Manager module: installs agent-browser and web-renderer, declares the extensions of the agent profile and a desktop entry.
self:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.programs.agent-browser;
in
{
  options.programs.agent-browser = {
    enable = lib.mkEnableOption "the agent Vivaldi (agent-browser) and the web renderer (web-renderer)";

    vivaldi = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      example = lib.literalExpression "pkgs.vivaldi";
      description = "Vivaldi to start. Null starts the `vivaldi` in PATH, so the one the system already has is used and none is installed in addition.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 9333;
      description = "DevTools port of the agent Vivaldi, on 127.0.0.1.";
    };

    extensions = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "bjfgambnhccakkhmkepdoekmckoijdlc" ];
      example = [
        "bjfgambnhccakkhmkepdoekmckoijdlc"
        "mdjildafknihdffpkfmmpnpoiajfjnjd"
      ];
      description = "Chrome Web Store IDs of extensions to install in the agent profile on its next start. The default is Browser MCP, which lets an agent drive this browser.";
    };

    desktopEntry = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Add a desktop entry \"Vivaldi (Agent)\" with its own icon, matched to the window by its class `agent-browser`.";
    };

    agentBrowserPackage = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      description = "The agent-browser command built from these options.";
    };

    webRendererPackage = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      description = "The web-renderer command built from these options.";
    };
  };

  config = lib.mkIf cfg.enable {
    programs.agent-browser = {
      agentBrowserPackage = pkgs.callPackage "${self}/agent-browser.nix" {
        vivaldiPackage = cfg.vivaldi;
        inherit (cfg) port;
      };
      webRendererPackage = pkgs.callPackage "${self}/web-renderer.nix" {
        agent-browser = cfg.agentBrowserPackage;
        inherit (cfg) port;
      };
    };

    home.packages = [
      cfg.agentBrowserPackage
      cfg.webRendererPackage
    ];

    # Chromium installs every extension listed in "External Extensions" under the user data dir (the same mechanism as programs.vivaldi.extensions).
    xdg.configFile = lib.listToAttrs (map (id: lib.nameValuePair "agent-browser/External Extensions/${id}.json" {
      text = builtins.toJSON { external_update_url = "https://clients2.google.com/service/update2/crx"; };
    }) cfg.extensions);

    xdg.desktopEntries = lib.mkIf cfg.desktopEntry {
      agent-browser = {
        name = "Vivaldi (Agent)";
        comment = "Vivaldi profile used by AI agents, with DevTools port";
        exec = "agent-browser";
        icon = "${self}/icon/agent-browser.png";
        categories = [ "Network" "WebBrowser" ];
      };
    };
  };
}
