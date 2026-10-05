# cc-swap

cc-swap lets you keep several Claude Code subscriptions on one machine and moves you between them before you hit a usage limit. It watches every account's 5-hour and 7-day usage, switches the account Claude Code is logged in as, and runs in the background so you don't have to think about it.

It is for you if you pay for more than one Claude plan and want to keep working instead of waiting for a limit to reset. It is a fork of [claude-swap](https://github.com/realiti4/claude-swap) and keeps everything upstream does, adding an always-on engine with the `maximize` strategy, a full-screen account view (Fleet), holds, notifications and diagnostics.

The full detail of every command, setting and behaviour is in the [reference](docs/reference.md). This page walks you through using it.

- [Install](#install)
- [Quick start](#quick-start)
- [Daily use](#daily-use)
- [How automatic switching decides](#how-automatic-switching-decides)
- [Common tasks](#common-tasks)
- [Troubleshooting](#troubleshooting)
- [More](#more)

## Install

You need:

- macOS or Linux for the background service. On Windows the commands work, but you run `cc-swap auto` in a terminal instead of a service.
- Python 3.12 or newer, and [uv](https://docs.astral.sh/uv/).
- Claude Code installed and logged in.

Install the latest version from GitHub:

```bash
uv tool install git+https://github.com/wonjun-lab/cc-swap
```

To pin a release, add its tag (releases are tagged `cc-vX.Y.Z`):

```bash
uv tool install git+https://github.com/wonjun-lab/cc-swap@cc-v0.5.5
```

The install gives you two commands, `cc-swap` and `cswap`. They are the same program; this guide uses `cc-swap`.

To upgrade later, run:

```bash
cc-swap upgrade           # install the latest release (restarts the service if you have one)
cc-swap upgrade --check   # only show whether a newer release exists and what changed
```

cc-swap is not on PyPI. Do not run `uv tool install claude-swap` or `pipx install claude-swap`: those install upstream claude-swap and replace cc-swap.

## Quick start

Follow these steps in order. When you finish, the engine is running and switches accounts for you.

**1. Log in to Claude Code with your first account** (run `claude`, then `/login`), and save it:

```bash
cc-swap add
```

**2. Log in as your next account and save it too.** In Claude Code, run `/login` and sign in with the other account, then:

```bash
cc-swap add
```

Do not run `/logout` before logging in as the next account. Current Claude Code may revoke the refresh token cc-swap just saved for the account you are leaving. Just `/login` over the top.

Repeat step 2 for every account. You can give an account a short name as you add it (`cc-swap add --alias work`) or later (`cc-swap alias 2 work`).

**3. Check what is left and let cc-swap do the rest:**

```bash
cc-swap init           # checklist: ok / FIX / TODO for each step
cc-swap init --apply   # set the strategy to maximize and install the service
```

`init` checks: Claude Code installed, logged in, the live login saved in a slot, two or more accounts, upstream claude-swap gone, strategy `maximize`, service running on this build, and priming off unless verified. `init --apply` does the two steps it can do for you: it sets `autoswitch.strategy` to `maximize` and runs `cc-swap service install` (the second only once you are logged in, the login is saved and upstream claude-swap is gone). Run `cc-swap init` again until every line says `ok`.

**4. Confirm it is running:**

```bash
cc-swap service status
cc-swap
```

`cc-swap` on its own opens Fleet, the account view. The top line tells you what automatic switching is doing, for example `Auto ON · using #1 main · 5h 62% past soft 50 — will switch to #2 side when you pause (forced at 98%, ~2h)`.

That's it. You don't need to restart Claude Code after a switch: on Linux and Windows it picks up the new account on your next message, and on macOS within about 30 seconds (the Keychain cache). Restart it, or reopen the VS Code extension tab, only if you want the change to apply instantly.

If you used upstream claude-swap before, read [Migrating from upstream cswap](#migrating-from-upstream-cswap) first.

## Daily use

### See where you stand

```bash
cc-swap list      # every account: 5h and 7d usage and reset times (and a login deadline that is close)
cc-swap status    # which account is active
cc-swap           # Fleet, the live view (also: cc-swap tui)
```

<img src="assets/fleet-wide.png" width="760" alt="Fleet: the status sentence and the attention line on top, the capacity summary, a table of accounts with 5h and 7d bars, reset times and status, the selected account in full, and the key footer">

Fleet shows, from top to bottom:

- **One sentence** saying what automatic switching is doing and who runs the engine (`viewer · service pid 4121 is switching`).
- **Attention lines**, only when something needs you, each with what to do about it: a login that needs renewing, priming paused (Claude Code killed by macOS at launch, an update settling, a new version not verified yet), a login that ends within a week, a locked keychain. One line, up to three when the table leaves rows over.
- **A capacity summary**: how many accounts still have 5h room, how long until the next account comes back and the next weekly reset, and roughly how many accounts' worth of weekly quota is left.
- **One row per account**: the order automatic switching would land on them in, exactly as the engine would (`●` is the active one, `·` only when forced, `–` not now, e.g. a reading too old to trust), the short name and slot number (`main #1`), plan, 5h and 7d bars with their reset times, and a status such as `next`, `last resort`, `excluded`, `login 3d left`, `re-login (r)`, `keychain locked (f)`, `reading 2h old`, `drain 18h` or `prime 19:30`. Each bar has an amber tick at the soft threshold and a red tick at the hard one.
- **The selected account in full**: organization, plan, login deadline, priming, and every usage window it has.

Fleet keys:

| Key | What it does |
|---|---|
| `enter` | Switch to the selected account |
| `r` | Re-login the selected account |
| `l` | Mark or unmark the selected account as last resort |
| `h` | Hold: stay on the active account for a while |
| `n` | Name the selected account (set its alias) |
| `↑`/`↓` or `j`/`k` | Move the selection |
| `m` | Menu: auto on/off, mode, swap strategy, prime now, fetch usage, exclude, account settings, engine log, switch history, classic dashboard |
| `?` | Explain everything on the screen |
| `q` | Quit |

Fleet is a viewer: it never takes over from the service. All menu letters are listed in the [reference](docs/reference.md#fleet-the-tui-home-for-maximize).

### Switch by hand

```bash
cc-swap switch              # rotate to the next account
cc-swap switch 2            # by slot number
cc-swap switch alice@example.com
cc-swap switch work         # by alias
```

Or select the account in Fleet and press `enter`. A manual switch works even while automatic switching is off, and it ends any hold.

### Name your accounts

```bash
cc-swap alias 2 work       # use "work" anywhere a number or email works
cc-swap alias 2 --unset    # back to the short name (the part before the @)
cc-swap alias              # list aliases
```

An alias may use letters, digits, `.`, `-` and `_`, and must not be only digits.

### Stay on one account for a while

A switch makes Claude Code re-read your whole conversation on the new account. In the middle of a long task you may prefer to stay put:

```bash
cc-swap hold 2h            # also 90m, 2h30m
cc-swap hold until 23:00   # until the next 23:00, local time
cc-swap hold off
cc-swap hold status
```

In Fleet, press `h`, then `h` (1 hour), `t` (2 hours), `f` (4 hours), `u` or type a time, or `o` (off). A hold lasts at most 24 hours and ends as soon as the active account changes. It only delays the "nice to have" moves: a hard threshold or 100% still moves you.

### Turn automatic switching off and on

```bash
cc-swap auto off      # stop switching and priming until you turn it back on
cc-swap auto on
cc-swap auto status   # on or off, who turned it off and when
```

Off is persistent and survives restarts. The engine keeps watching (so `cc-swap why` can still tell you what it would do), but switches nothing. In Fleet: `m` then `o`.

### See what happened

```bash
cc-swap history         # the last 20 switches: when, #from -> #to, why, who
cc-swap history -n 0    # all of them
cc-swap why             # why the engine did or didn't switch just now
```

### Notifications

The engine sends a desktop notification when it switches, when a login needs renewing or ends within 24 hours, when priming pauses after a Claude Code update, and when the macOS Keychain stays unreadable. macOS uses `osascript`; Linux uses `notify-send` (on Debian and Ubuntu, install `libnotify-bin`).

```bash
cc-swap notify test                      # send one now
cc-swap notify status                    # which kinds are on
cc-swap config set notify.switch false   # everything except switch notices
cc-swap config set notify.enabled false  # none at all
```

## How automatic switching decides

This section explains the `maximize` strategy in plain terms. The [reference](docs/reference.md#the-maximize-strategy) states every rule exactly.

### Soft and hard thresholds

Each window (5-hour and 7-day) has two thresholds on the active account:

| | 5h | 7d | What happens |
|---|---|---|---|
| Soft | 50% | 90% | Switch at the next moment you are idle |
| Hard | 95% | 98% | Switch now |

"Idle" means your usage barely moved over the last 10 minutes (at most 1 percentage point in both windows). Switching while you pause means no turn of yours is interrupted. At 100% it switches at once, whatever else is going on. It also switches if the hard threshold is less than 10 minutes away at your recent pace.

A target account has to sit at least 5 points below both soft thresholds, so you don't land somewhere you would leave again straight away.

### Which account comes next

Accounts are ranked by how much of their weekly quota would otherwise go to waste:

```
score = (100 − 7d used %) ÷ (days until the 7d reset × 100/7)
```

A score above 1 means the account has more weekly quota left than an even pace would use before it resets. That quota expires at the reset if nobody uses it, so it goes first.

For example, account A has used 70% of its week and resets in 12 hours. Account B has used 10% and resets in 6 days.

- A: 30 ÷ (0.5 × 14.3) = **4.2**
- B: 90 ÷ (6 × 14.3) = **1.05**

A goes first, even though B has far more left: B still has six days to use its quota, A has half a day. When scores are within 0.1 of each other, 20x plans go first, then the account whose 5h window resets soonest, then the lower slot number.

Even below the soft thresholds, cc-swap moves you to a better-scored account at an idle moment (at most once every 30 minutes). Once it has learned your usual quiet hours, it can also move you early when the active account's week is on pace to cross its soft threshold before your next quiet time, and it saves small rebalancing moves for a quiet time.

### Last-resort and excluded accounts

```bash
cc-swap last-resort add carol@example.com   # use only when nothing else can take you
cc-swap last-resort remove carol@example.com
cc-swap disable 3                           # never switch to it automatically
cc-swap enable 3
```

A last-resort account is used only when no normal account can take you. An excluded (disabled) account is never a target and never primed, though you can still `cc-swap switch` to it yourself. In Fleet, `l` toggles last resort and `m` → `x` excludes or includes the selected account.

### Waiting for a reset

If the window that crossed its threshold resets within 15 minutes and your pace won't reach 100% before then, cc-swap waits for the reset instead of switching, because a switch costs a context re-read and the reset is about to fix the problem anyway. Fleet says so: `5h 96% — resets in 8m, waiting it out (switches at once if it hits 100%)`. If you hit 100% it switches immediately.

### Using up a week before it resets

An account near its weekly (7d) reset whose quota would otherwise expire unused is *draining*: within 24 hours of the reset (`maximize.drainHours`; 0 turns it off), cc-swap sets its 7d soft threshold aside and keeps using it up to the 98% hard threshold; while it still has at least a quarter 5h window of room it lands on it up to 93% and tries it first, the soonest reset first. Fleet tags it `drain 18h`, and `cc-swap why` says `#1 7d 86% resets in 18h — draining it first`. Hard thresholds, 100% and holds work as always.

If you raise the 7d hard threshold to 99 or more, cc-swap may keep using the account for a few more minutes once it gets there, because the API reports whole percents and up to a point of quota is still left. It learns how long it can safely ride that last point.

### Priming (optional, off by default)

A 5-hour window only starts with an account's first request. An account you haven't touched since its last reset starts its five hours only when you switch to it. With priming on, cc-swap sends each idle account one tiny request ("Reply OK") shortly after its window resets, so the window is already running when you need the account. That way you can get up to twice the 5h quota in the same five hours.

Priming is **off by default** because it makes automated requests on your subscriptions, which Anthropic's consumer terms may not allow. Read the [terms of service note](docs/reference.md#5h-window-priming) before you turn it on:

```bash
cc-swap config set prime.enabled true
```

**After every Claude Code update**, priming pauses itself until its isolation from your login is verified again. The engine does that on its own: it runs the same zero-cost check as `cc-swap prime verify`, resumes priming when it passes and sends a notification either way (`cc-swap config set prime.autoVerify false` turns this off). Meanwhile Fleet shows `! priming paused: claude 2.1.3 -> 2.1.4 (the engine re-verifies it; or cc-swap prime verify)`. To do it yourself, run:

```bash
cc-swap prime verify
```

It costs nothing (no real request is made) and resumes priming on the engine's next tick when every check passes. If a check fails (yours or the engine's), priming stays paused until a manual `cc-swap prime verify` passes; turn it off with `cc-swap config set prime.enabled false` and open an issue with your `claude --version`.

cc-swap never updates Claude Code itself: Claude Code's own updater does, and the guard notices the new `claude` file. It also waits 10 minutes after Claude Code changed before the engine runs it (`claude.settleS`), logs every `claude` it runs to `claude-exec.jsonl`, and if macOS starts killing `claude` at launch (SIGKILL, exit 137), `cc-swap doctor` says so and prints the fix ([reference](docs/reference.md#every-claude-cc-swap-runs)).

## Common tasks

### Change the thresholds while it runs

The engine, including the service, picks changes up on its next check, with no restart:

```bash
cc-swap config set maximize.soft5h 60
cc-swap config set maximize.hard7d 99
cc-swap config                 # list every setting and its value
cc-swap config unset maximize.soft5h   # back to the default
```

A soft threshold can't be set above its hard one. In Fleet, `m` → `s` (Swap strategy) edits the thresholds and the other knobs with a live preview of what the engine would decide; `s` saves.

### Renew a login before it expires

Every Claude Code login has a fixed deadline, about 27 to 30 days after you logged in. Using the account does not extend it. Once it passes, the account stops working until you log in again. cc-swap warns you from 7 days out: Fleet tags the account `login 3d left`, `cc-swap list` shows `login expires …`, and you get a notification in the last 24 hours.

To renew (early, or once it has expired), run `cc-swap login 4`, or in Fleet select the account and press `r`. cc-swap launches Claude Code's own login for that account's email (`claude auth login`) in a throwaway profile; you only sign in in the browser (over SSH, open the printed URL on any device and paste the code back). It stores the new login into the slot only if it is that account, and leaves the login you are using alone, unless the account you renewed is the one you are on: then that gets the new login too. Ctrl-C cancels and stores nothing.

When claude can't be launched (not found, or too old for `claude auth login`), cc-swap shows the manual steps instead: run `claude`, `/login` as that account, then `cc-swap add`. cc-swap recognises the account and updates its slot instead of adding a duplicate.

To add another account the same way without touching the login you are using, run `cc-swap login --new` (Fleet: `m` → `a` → `s`). It is stored in the next free slot (`--slot N` to choose); an account cc-swap already has is refused with its `cc-swap login N`.

Two places holding the same login (two slots, or a slot and another slot's `cswap run` profile) cannot both stay logged in: a refresh token works once. `cc-swap doctor` reports it as `shared-login`, and cc-swap does not refresh that login until you re-login one of them.

### Use several machines

Each machine keeps its own logins. Log in and `cc-swap add` on every machine separately, and renew logins on each machine that needs it rather than copying one login between machines. Usage from all machines counts against the same quota, and cc-swap only trusts what the server reports, so the engines don't need to coordinate. With priming on, each machine waits a random 45 to 300 seconds after a reset before priming, and a machine skips an account whose window another machine already opened.

If two machines share logins (moved with export/import) they also share one usage-reading budget. You can let one machine poll and hand its readings to the other:

```bash
cc-swap list --json | ssh laptop cc-swap import-usage - --hold 600
```

### Run two accounts at the same time

`cc-swap run` starts Claude Code as a given account in the current terminal only. Every other terminal and the VS Code extension stay on your default account.

```bash
cc-swap run 2                   # account 2, this terminal only
cc-swap run 2 -- --resume       # pass arguments to claude after --
cc-swap run 2 --share-history   # see your normal chat history too
```

You can map a directory to an account, so a bare `cc-swap run` there picks it: `cc-swap map 2 ~/work/client-app`.

### Back up and restore accounts

```bash
cc-swap export backup.cswap          # all accounts, as plaintext JSON
cc-swap import backup.cswap          # skips accounts that already exist
cc-swap import backup.cswap --force  # overwrite existing ones
```

The file contains your logins in plain text. Encrypt it if it leaves the machine, for example `cc-swap export - | gpg -c > backup.gpg`.

### Migrating from upstream cswap

cc-swap uses the same data as claude-swap, so your accounts carry over without logging in again. The two must not run side by side, because two engines would fight over the active account.

1. Stop everything upstream runs: TUI windows, `cswap auto` loops, cron jobs running `cswap auto --once`, and the menu bar's *Auto-switch accounts* toggle.
2. `uv tool uninstall claude-swap`
3. `uv tool install git+https://github.com/wonjun-lab/cc-swap`
4. `cc-swap init --apply`, then `cc-swap init` until every step is ok.

### Going back to upstream

```bash
cc-swap service uninstall
uv tool uninstall cc-swap
uv tool install claude-swap
```

Upstream warns about the unknown `maximize` strategy, falls back to `best`, and ignores the cc-swap settings.

### Uninstall

```bash
cc-swap service uninstall   # stop and remove the background service
cc-swap purge               # remove all stored accounts and settings
uv tool uninstall cc-swap
```

Skip `purge` if you want to keep your accounts for later or for upstream claude-swap.

## Troubleshooting

Start with these two. Both are read-only and safe to paste into an issue (accounts appear by slot number, never by email or token).

```bash
cc-swap doctor   # checks this machine and every login; one fix line per problem
cc-swap why      # the engine's last decision and its reason code, explained
```

**It didn't switch.** Run `cc-swap why`. Common answers: `maximize-pending` (a soft threshold is crossed and it is waiting for you to pause), `reset-wait` (the window resets soon), `hold` (you held the account), `auto-off` (automatic switching is off), `no-qualifying-candidate` (no other account is far enough below the thresholds). Every code is explained in the [reference](docs/reference.md#why-didnt-it-switch).

**Nothing switches at all.** Check that an engine is running: `cc-swap service status`. Fleet's top line reads `Not switching — no engine is running` when none is (`Not switching — the service is stopped` when it is installed but not running). Run `cc-swap service install` (or `cc-swap init --apply`): an engine started from Fleet's menu stops when Fleet quits.

**"Keychain unreadable; holding" or `rc=36` (macOS).** The login Keychain is locked, the session can't reach it (for example over SSH), or a `/login` is still being written. cc-swap stops switching rather than overwrite a login it can't see, and resumes by itself once the Keychain answers. Unlock the login keychain. If it lasts more than 15 minutes you get a notification and the service restarts itself.

**`re-login needed — login expired` or `invalid_grant`.** The login passed its fixed deadline, or its refresh token was revoked. Log in again: `cc-swap login N`, or Fleet → select the account → `r`.

**`unmanaged login, run cc-swap add`.** You are logged in to Claude Code as an account cc-swap doesn't have, or as a different account than the slot expects. Check which account you are logged in as, then run `cc-swap add`.

**`mixed live login` (macOS).** A `/login` ran while the login Keychain was locked (for example over SSH): Claude Code saved the new login in plaintext (`~/.claude/.credentials.json`) and `~/.claude.json` names it, but the Keychain still holds the previous account's token, so the engine holds and `cc-swap add` refuses. Run `cc-swap repair-live` in a GUI terminal: it checks whose the plaintext login is, asks, then writes it into the Keychain and its slot and removes the plaintext file.

**Priming paused after an update.** Expected after every Claude Code update; the engine re-verifies on its own within a tick or two. If it says the automatic verify failed, run `cc-swap prime verify`.

**`cc-swap upgrade` says `cannot confirm the latest release`.** It could not reach GitHub (often the 60-requests-an-hour anonymous rate limit) and would not guess. It changed nothing. Wait and retry, or set `GITHUB_TOKEN` (or log in with the GitHub CLI, `gh auth login`) so the lookup is authenticated. `cc-swap upgrade --check` exits `2` in this case (`0` up to date, `10` update available, `1` no release published), so a script never mistakes a failed check for being current.

**`cc-swap auto` exits with code 4.** Another engine, usually the service, is already running on this machine. Only one runs at a time. Use the service, or run `cc-swap service uninstall` first.

## More

- [Reference](docs/reference.md): every command and flag, every setting, the full reason-code table, Fleet in detail, JSON output for scripts, data locations and how it works.
- [Releasing](docs/releasing.md): for maintainers.

## License

MIT. cc-swap is a fork of [claude-swap](https://github.com/realiti4/claude-swap) by realiti4; all of upstream's work is theirs.
