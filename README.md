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
| `bootstrap.sh` | Sets up a new computer. Run it once. |
| `rebuild.sh` | Applies your setup. Run it after every change. |
| `flake.lock` | Pins the exact versions of everything, so each computer gets the same result. |
| `scripts/` | Two tools: a check that warns in new terminals when your setup needs attention, and `sync-repos`. |
| `home/.config/sync-repos/config.yaml` | The folders `sync-repos` keeps up to date. |
| `.github/workflows/update-flake-lock.yml` | Updates `flake.lock` on GitHub every day. |

On macOS, [nix-darwin](https://github.com/nix-darwin/nix-darwin) applies `darwin.nix` and `home.nix`.
On Linux, [Home Manager](https://github.com/nix-community/home-manager) applies `home.nix`.

## Set up a computer

1. Make sure you have `git`.
   On macOS, run `xcode-select --install`. On Linux, install it with your package manager.
2. Fork this repository on GitHub, so you can save your own changes. Then clone your fork. Any folder works:
   ```sh
   git clone https://github.com/YOUR-NAME/my-setup-template.git ~/my-setup
   ```
3. Run the bootstrap script:
   ```sh
   ~/my-setup/bootstrap.sh
   ```
   It installs Nix, links the folder to `~/.my-setup`, and offers to set your username in `flake.nix`.
   Then it applies the setup. On macOS, it asks for your password.
4. Commit and push the username change:
   ```sh
   cd ~/my-setup
   git commit -am "Set my username"
   git push
   ```
5. Open a new terminal. Run `ll` to check that it worked.

If you already have files that the setup manages, such as `~/.bashrc` or `~/.zshrc`, they get renamed with a `.backup` ending.
Copy anything you want to keep from them into `home.nix`.

If you move the folder later, run `./bootstrap.sh` again to update the link.

## Change your setup

Edit a file, then run `./rebuild.sh`.

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
If you add a new file, run `git add` on it before `./rebuild.sh`.

## Messages in new terminals

Each new terminal checks your setup and prints a line when something needs attention:

```text
my-setup: the active setup differs from origin/main. Run ~/.my-setup/rebuild.sh
my-setup: 2 uncommitted changes. Run git -C ~/.my-setup status
```

- The first line means your computer isn't running what's on `origin/main`.
  If `origin/main` has new commits, the line tells you to pull first.
  Changes to files that don't affect the setup, like this README, don't count.
- The second line means you have edits you haven't committed yet. Commit and push them so they don't get lost.

When everything matches, new terminals print nothing.

## Sync repos

`sync-repos` keeps every git repo under the folders in `home/.config/sync-repos/config.yaml` up to date.
It starts with `~/Codes`. Edit the list to match where you keep your code.
Edits take effect right away, without `./rebuild.sh`.

For each repo, it fetches every remote, then:

- It fast-forwards branches that are behind their upstream. It never touches a branch that has its own commits, and never a checkout with uncommitted changes.
- It deletes a local branch once its work is in the remote's default branch, whether by merge, rebase, or squash. It never deletes a remote branch.
- It keeps any branch it can't prove was merged.
  To spot squash merges more often, install [`gh`](https://cli.github.com) and sign in. `sync-repos` then checks GitHub for merged pull requests.

Each deleted branch is logged with its commit to `~/.local/state/sync-repos/deleted-branches.tsv`.
To bring one back, run `git branch NAME SHA`.

It runs at login and every hour.
On macOS, the latest run is logged to `~/Library/Logs/sync-repos.log`.
On Linux, run `journalctl --user -u sync-repos` to see the log.

You can also run it yourself:

```sh
sync-repos          # the folders in the config
sync-repos DIR ...  # these folders instead
sync-repos -n       # fetch only, change no branches
```

## Automatic updates

Every day, a GitHub Action updates `flake.lock` to the newest versions.
It checks that the updated setup still builds, then merges it.
If the build fails, it leaves the pull request open with a comment.

GitHub turns this off in forks until you allow it:

1. In your fork, open the **Actions** tab and enable workflows.
2. Go to **Settings > Actions > General**. Under **Workflow permissions**, check **Allow GitHub Actions to create and approve pull requests**.

To get the updates on a computer, run `git pull`, then `./rebuild.sh`.
A new terminal reminds you when an update is waiting.

To update right away without waiting for GitHub:

```sh
nix flake update
./rebuild.sh
```

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

## Tests

Both tools have tests that run against throwaway git repos:

```sh
nix develop -c python3 -m unittest discover -s scripts -p 'test_*.py'
```

## License

[MIT](LICENSE). Use it, change it, and share it however you like.
