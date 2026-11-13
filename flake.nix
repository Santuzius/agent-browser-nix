{
  description = "Browser tools for AI agents: a Vivaldi profile agents can drive, and a renderer that reads web pages as Markdown";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      packages.${system} = rec {
        agent-browser = pkgs.callPackage ./agent-browser.nix { };
        web-renderer = pkgs.callPackage ./web-renderer.nix { inherit agent-browser; };
        default = web-renderer;
      };

      homeManagerModules.default = import ./module.nix self;
    };
}
