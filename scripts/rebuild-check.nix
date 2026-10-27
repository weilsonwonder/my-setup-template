{ pkgs, ... }:

let
  rebuildCheck = pkgs.writers.writePython3Bin "rebuild-check" {
    flakeIgnore = [ "E501" ];
    makeWrapperArgs = [ "--prefix" "PATH" ":" "${pkgs.git}/bin" ];
  } (builtins.readFile ./rebuild-check.py);
  runOnNewTerminal = ''
    if [[ -t 1 ]]; then rebuild-check; fi
  '';
in

{
  home.packages = [ rebuildCheck ];
  programs.bash.initExtra = runOnNewTerminal;
  programs.zsh.initContent = runOnNewTerminal;
}
