{ username, ... }:

{
  nix.enable = false;
  nixpkgs.hostPlatform = "aarch64-darwin";
  system.stateVersion = 6;

  system.primaryUser = username;
  users.users.${username}.home = "/Users/${username}";

  system.defaults = {
    dock.autohide = true;
    finder.AppleShowAllExtensions = true;
    NSGlobalDomain.KeyRepeat = 2;
  };
}
