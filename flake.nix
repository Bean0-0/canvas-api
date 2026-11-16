{
  description = "HTTP API for Canvas LMS backed by a logged-in headless browser";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: { default = pkgs.callPackage ./nix/package.nix { }; });

      nixosModules.default = ./nix/module.nix;

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          inputsFrom = [ self.packages.${pkgs.system}.default ];
          packages = [ pkgs.python3Packages.pytest pkgs.python3Packages.httpx ];
          PLAYWRIGHT_BROWSERS_PATH = self.packages.${pkgs.system}.default.browsers;
        };
      });
    };
}
