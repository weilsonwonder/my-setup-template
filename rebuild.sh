#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$DIR"
if [ ! -e ~/.my-setup ]; then
  echo "The ~/.my-setup link is missing or points nowhere. Run ./bootstrap.sh to link this checkout." >&2
  exit 1
fi

case "$(uname -s)" in
  Darwin)
    if command -v darwin-rebuild >/dev/null; then
      sudo darwin-rebuild switch --flake .#mac
    else
      sudo "$(command -v nix)" run nix-darwin -- switch --flake .#mac
    fi
    PROFILE="/etc/profiles/per-user/$(id -un)"
    ;;
  Linux)
    if command -v home-manager >/dev/null; then
      home-manager switch -b backup --flake ".#$(uname -m)-linux"
    else
      nix run home-manager -- switch -b backup --flake ".#$(uname -m)-linux"
    fi
    PROFILE="$HOME/.nix-profile"
    ;;
  *)
    echo "Unsupported system: $(uname -s)" >&2
    exit 1
    ;;
esac

"$PROFILE/bin/rebuild-check" --record "$DIR"
