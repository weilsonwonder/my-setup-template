#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

username="$(sed -nE 's/^ *username = "(.*)";/\1/p' flake.nix)"
if [ "$username" != "$(id -un)" ]; then
  echo "flake.nix has username = \"$username\", but you are \"$(id -un)\". Edit flake.nix first." >&2
  exit 1
fi

case "$(uname -s)" in
  Darwin)
    if command -v darwin-rebuild >/dev/null; then
      sudo darwin-rebuild switch --flake .#mac
    else
      sudo "$(command -v nix)" run nix-darwin -- switch --flake .#mac
    fi
    ;;
  Linux)
    if command -v home-manager >/dev/null; then
      home-manager switch -b backup --flake ".#$(uname -m)-linux"
    else
      nix run home-manager -- switch -b backup --flake ".#$(uname -m)-linux"
    fi
    ;;
esac
