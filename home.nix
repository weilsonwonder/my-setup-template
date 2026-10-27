{ pkgs, ... }:

{
  home.stateVersion = "26.11";

  home.packages = with pkgs; [
    ripgrep
    fd
    jq
  ];

  home.shellAliases = {
    ll = "ls -lh";
  };

  programs.git.enable = true;
  programs.bash.enable = true;
  programs.zsh.enable = true;
  programs.home-manager.enable = true;

  news.display = "silent";
}
