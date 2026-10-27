# my-setup-template

Set up your computer from a few text files, using [Nix](https://nixos.org).

You list the tools, shell settings, and system preferences you want.
One command makes your computer match the list.
On a new computer, the same command gives you the same setup.

It works on macOS (Apple silicon) and on any Linux distribution.

## What's inside

| File | What it does |
| --- | --- |
| `flake.nix` | Holds your username and connects the other files. |
| `home.nix` | Your tools and shell settings. Used on macOS and Linux. |
| `darwin.nix` | macOS system settings, like the Dock and key repeat. Used on macOS only. |
| `switch.sh` | Applies your setup. Run it after every change. |
| `flake.lock` | Pins the exact versions of everything, so each computer gets the same result. |

On macOS, [nix-darwin](https://github.com/nix-darwin/nix-darwin) applies `darwin.nix` and `home.nix`.
On Linux, [Home Manager](https://github.com/nix-community/home-manager) applies `home.nix`.

## Set up a computer

1. Install Nix:
   ```sh
   curl -fsSL https://install.determinate.systems/nix | sh -s -- install
   ```
   Then open a new terminal.
2. Fork this repository on GitHub, so you can save your own changes. Then clone your fork.
   This command uses the `git` that comes with Nix, so it works before you install anything else:
   ```sh
   nix run nixpkgs#git -- clone https://github.com/YOUR-NAME/my-setup-template.git ~/my-setup
   cd ~/my-setup
   ```
3. Open `flake.nix` and change `username = "alice";` to your username.
   To see your username, run `whoami`.
4. Apply the setup:
   ```sh
   ./switch.sh
   ```
   On macOS, it asks for your password.
5. Open a new terminal. Run `ll` to check that it worked.

If you already have files that the setup manages, such as `~/.bashrc` or `~/.zshrc`, they get renamed with a `.backup` ending.
Copy anything you want to keep from them into `home.nix`.

## Change your setup

Edit a file, then run `./switch.sh` again.

For example, to install `htop`, add it to `home.packages` in `home.nix`:

```nix
home.packages = with pkgs; [
  ripgrep
  fd
  jq
  htop
];
```

To find what you can add:

- Packages: [search.nixos.org/packages](https://search.nixos.org/packages)
- Settings for `home.nix`: [Home Manager options](https://home-manager-options.extranix.com)
- Settings for `darwin.nix`: [nix-darwin options](https://nix-darwin.github.io/nix-darwin/manual/)

Nix only reads files that git knows about.
If you add a new file, run `git add` on it before `./switch.sh`.

## Update everything

```sh
nix flake update
./switch.sh
```

This updates `flake.lock` to the newest versions. Commit it so your other computers get the same versions.

## Undo a change

On macOS:

```sh
sudo darwin-rebuild --rollback
```

On Linux, list your past setups, then run `activate` in the one you want to go back to:

```sh
home-manager generations
/nix/store/...-home-manager-generation/activate
```

## License

[MIT](LICENSE). Use it, change it, and share it however you like.
