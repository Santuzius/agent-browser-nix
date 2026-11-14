# Home Manager module: installs agent-browser and web-renderer, declares the extensions of the agent profile, a desktop entry and (with plasma-manager) a KWin rule that starts the window minimized.
self:
{
  config,
  options,
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

    startMinimized = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "With plasma-manager, add a KWin rule \"Vivaldi (Agent): start minimized\" for windows of class `agent-browser`. Every value is a `lib.mkDefault`, so settings dumped by `nix run github:nix-community/plasma-manager` override it instead of conflicting.";
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

  config = lib.mkIf cfg.enable (lib.mkMerge [ {
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
  }

  # Written as plain configFile entries instead of programs.plasma.window-rules: window-rules sets the same keys at normal priority (General.rules as a string where the plasma-manager dump has an integer), which conflicts with a dump. The group has its own name instead of a number so it does not merge into other rules; a dump or window-rules that sets General.rules at normal priority overrides the list.
  (lib.optionalAttrs (options ? programs.plasma.configFile) {
    programs.plasma.configFile.kwinrulesrc = lib.mkIf cfg.startMinimized (lib.mapAttrsRecursive (_: lib.mkDefault) {
      agent-browser = {
        Description = "Vivaldi (Agent): start minimized";
        minimize = true;
        minimizerule = 3;
        wmclass = "agent-browser";
        wmclasscomplete = false;
        wmclassmatch = 1;
      };
      General = {
        count = 1;
        rules = "agent-browser";
      };
    });
  })
  ]);
}
