#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

echo "==> Step 1: install Nix"
if command -v nix >/dev/null; then
  echo "    Nix is already installed, skipping"
else
  curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install --no-confirm
  export PATH="/nix/var/nix/profiles/default/bin:$PATH"
fi

echo "==> Step 2: link this checkout to ~/.my-setup"
ln -sfn "$DIR" ~/.my-setup

echo "==> Step 3: set your username in flake.nix"
REAL_USER="$(id -un)"
FLAKE_USER="$(sed -nE 's/^[[:space:]]*username = "([^"]+)";.*/\1/p' "$DIR/flake.nix" | head -n1)"
if [ -z "$FLAKE_USER" ]; then
  echo "    Could not find the username = \"...\"; line in flake.nix. Add it back, then run ./bootstrap.sh again." >&2
  exit 1
elif [ "$FLAKE_USER" = "$REAL_USER" ]; then
  echo "    flake.nix already has username = \"$REAL_USER\""
else
  read -r -p "    flake.nix has username = \"$FLAKE_USER\", but you are \"$REAL_USER\". Change it to \"$REAL_USER\"? [y/N] " REPLY
  if [ "$REPLY" != "y" ] && [ "$REPLY" != "Y" ]; then
    echo "    Set username in flake.nix yourself, then run ./bootstrap.sh again." >&2
    exit 1
  fi
  sed -i.bak -E "s/^([[:space:]]*username = \")[^\"]+(\";.*)/\1${REAL_USER}\2/" "$DIR/flake.nix"
  rm "$DIR/flake.nix.bak"
  echo "    Changed. Commit it so your other computers get it too."
fi

echo "==> Step 4: apply the setup"
"$DIR/rebuild.sh"

echo "==> Done. Open a new terminal. After this, run ./rebuild.sh after every change."
