{ config, lib, pkgs, ... }:

let
  syncRepos = pkgs.writers.writePython3Bin "sync-repos" {
    libraries = [ pkgs.python3Packages.pyyaml ];
    flakeIgnore = [ "E501" ];
  } (builtins.readFile ./sync-repos.py);
  profileBin = "${config.home.profileDirectory}/bin";
  runOnMac = pkgs.writeShellScript "sync-repos-scheduled" ''
    exec >"$HOME/Library/Logs/sync-repos.log" 2>&1
    export PATH="/usr/bin:/bin:/usr/sbin:/sbin:${profileBin}"
    date
    exec ${syncRepos}/bin/sync-repos
  '';
in

lib.mkMerge [
  {
    home.packages = [ syncRepos ];
    xdg.configFile."sync-repos/config.yaml".source =
      config.lib.file.mkOutOfStoreSymlink "${config.home.homeDirectory}/.my-setup/home/.config/sync-repos/config.yaml";
  }

  (lib.mkIf pkgs.stdenv.hostPlatform.isDarwin {
    launchd.agents.sync-repos = {
      enable = true;
      config = {
        ProgramArguments = [ "${runOnMac}" ];
        RunAtLoad = true;
        StartCalendarInterval = [ { Minute = 0; } ];
      };
    };
  })

  (lib.mkIf pkgs.stdenv.hostPlatform.isLinux {
    systemd.user.services.sync-repos = {
      Unit.Description = "Fetch and fast-forward the git repos listed in ~/.config/sync-repos/config.yaml";
      Service = {
        Type = "oneshot";
        Environment = "PATH=${profileBin}:/usr/bin:/bin";
        ExecStart = "${syncRepos}/bin/sync-repos";
      };
    };
    systemd.user.timers.sync-repos = {
      Unit.Description = "Run sync-repos at login and every hour";
      Timer = {
        OnStartupSec = "1min";
        OnCalendar = "hourly";
        Persistent = true;
      };
      Install.WantedBy = [ "timers.target" ];
    };
  })
]
