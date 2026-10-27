{
  description = "my-setup-template";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    home-manager.url = "github:nix-community/home-manager";
    home-manager.inputs.nixpkgs.follows = "nixpkgs";
    nix-darwin.url = "github:nix-darwin/nix-darwin";
    nix-darwin.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { nixpkgs, home-manager, nix-darwin, ... }:
    let
      username = "alice";
    in
    {
      darwinConfigurations.mac = nix-darwin.lib.darwinSystem {
        specialArgs = { inherit username; };
        modules = [
          ./darwin.nix
          home-manager.darwinModules.home-manager
          {
            home-manager.useGlobalPkgs = true;
            home-manager.useUserPackages = true;
            home-manager.backupFileExtension = "backup";
            home-manager.users.${username} = ./home.nix;
          }
        ];
      };

      homeConfigurations = nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ] (system:
        home-manager.lib.homeManagerConfiguration {
          pkgs = nixpkgs.legacyPackages.${system};
          modules = [
            ./home.nix
            {
              home.username = username;
              home.homeDirectory = "/home/${username}";
              targets.genericLinux.enable = true;
            }
          ];
        });
    };
}
