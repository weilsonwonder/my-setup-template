# my-setup-template

Set up your computer from a few text files, using [Nix](https://nixos.org).

You list the tools, shell settings, and system preferences you want.
One command makes your computer match the list.
On a new computer, the same command gives you the same setup.

It works on macOS (Apple silicon) and on any Linux distribution.

## Setup

1. Install `git`. On macOS:
   ```sh
   xcode-select --install
   ```
   On Linux, use your package manager.
2. Fork this repository on GitHub, so you can save your own changes.
3. Clone your fork and bootstrap:
   ```sh
   git clone https://github.com/YOUR-NAME/my-setup-template.git ~/my-setup
   cd ~/my-setup && ./bootstrap.sh
   ```
   It installs Nix, links the folder to `~/.my-setup`, offers to set your username in `flake.nix`, then applies the setup. On macOS, it asks for your password.
   Existing files the setup manages, such as `~/.zshrc`, get renamed with a `.backup` ending. Copy anything you want to keep into `home.nix`.
4. Save the username change:
   ```sh
   git commit -am "Set my username"
   git push
   ```
5. Open a new terminal and run `ll` to check that it worked.

## Make changes

Edit `home.nix` for tools and shell settings, or `darwin.nix` for macOS settings, then run `./rebuild.sh`.
Nix only reads files that git knows about, so `git add` any new file before rebuilding.

To find what you can add:

- Packages: [search.nixos.org/packages](https://search.nixos.org/packages)
- Settings for `home.nix`: [Home Manager options](https://home-manager-options.extranix.com)
- Settings for `darwin.nix`: [nix-darwin options](https://nix-darwin.github.io/nix-darwin/manual/)

## Commands

| Command | What it does |
| --- | --- |
| `./bootstrap.sh` | Sets up a new computer. Run it again if you move the folder. |
| `./rebuild.sh` | Applies your setup. Run it after every change. |
| `sync-repos` | Fetches every repo under the folders in `home/.config/sync-repos/config.yaml`, fast-forwards branches that are behind, and deletes local branches that are already merged. Runs at login and every hour. `-n` fetches only. |
| `rebuild-check` | Runs in each new terminal. Prints a line when the computer doesn't match `origin/main` or the folder has uncommitted changes. |
| `ll` | `ls -lh` |

## Automatic updates

A daily GitHub Action updates `flake.lock` to the newest versions and merges it if the setup still builds.
To get the updates on a computer, run `git pull`, then `./rebuild.sh`.

GitHub turns this off in forks. To turn it on:

1. In your fork, open the **Actions** tab and enable workflows.
2. Go to **Settings > Actions > General**. Under **Workflow permissions**, check **Allow GitHub Actions to create and approve pull requests**.

To update right away:

```sh
nix flake update
./rebuild.sh
```

## License

[MIT](LICENSE). Use it, change it, and share it however you like.
