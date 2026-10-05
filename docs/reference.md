# cc-swap reference

This is the complete reference for cc-swap. For a guided introduction, start with the [README](../README.md). Everything the README simplifies is stated exactly here.

cc-swap installs two console scripts, `cc-swap` and `cswap`, that run the same program; help output names whichever one you typed. Commands written as `cswap …` (including in upstream claude-swap's documentation) work unchanged. cc-swap is installed from git, never from PyPI: where upstream instructions say `uv tool install claude-swap`, `pipx install claude-swap`, `uv tool upgrade claude-swap` or `uv tool uninstall claude-swap`, use `uv tool install git+https://github.com/wonjun-lab/cc-swap`, `cc-swap upgrade` and `uv tool uninstall cc-swap`; the upstream commands would replace cc-swap with upstream.

Contents:

- [Commands](#commands)
- [Settings](#settings)
- [The `maximize` strategy](#the-maximize-strategy)
- [The `best` and `consume-first` strategies](#the-best-and-consume-first-strategies)
- [5h window priming](#5h-window-priming)
- [Always-on service](#always-on-service)
- [Turning automatic switching off: `cc-swap auto off`](#turning-automatic-switching-off-cc-swap-auto-off)
- [Staying on one account: `cc-swap hold`](#staying-on-one-account-cc-swap-hold)
- [Switch history: `cc-swap history`](#switch-history-cc-swap-history)
- [Desktop notifications: `cc-swap notify`](#desktop-notifications-cc-swap-notify)
- [Fleet: the TUI home for maximize](#fleet-the-tui-home-for-maximize)
- [Classic dashboard and watch view](#classic-dashboard-and-watch-view)
- [Diagnostics: `doctor`, `init`, `why`](#diagnostics-doctor-init-why)
- [Managing accounts](#managing-accounts)
- [Session mode: run several accounts at once](#session-mode-run-several-accounts-at-once)
- [Backup and migration](#backup-and-migration)
- [Share usage readings between machines](#share-usage-readings-between-machines)
- [JSON output for scripting](#json-output-for-scripting)
- [Installing, upgrading and migrating](#installing-upgrading-and-migrating)
- [Menu bar (macOS)](#menu-bar-macos)
- [How it works](#how-it-works)
- [Data locations](#data-locations)

## Commands

`cc-swap help` (or `cc-swap --help`) prints this list. Most commands take `--help` for their own flags. `--debug` enables debug logging on any command. The original flag spellings (`cc-swap --switch`, `cc-swap --list`, ...) keep working. Aliases: `ls` = `list`, `rm` = `remove`, `update` = `upgrade`.

### Accounts

| Command | What it does |
|---|---|
| `cc-swap add` | Save the account Claude Code is logged in as (updates the slot if the account is already stored). `--slot N` picks the slot (prompts before overwriting), `--alias NAME` sets an alias |
| `cc-swap add-token [TOKEN\|-]` | Register a setup-token or API key without a browser login. `--slot N`, `--email EMAIL`; `-` reads stdin. See [below](#add-an-account-from-a-raw-token-or-api-key) |
| `cc-swap login <num\|email>` | Re-login an account: runs `claude auth login --email <its email>` in a throwaway profile, checks the new login is that account (email, organization, account id) and stores it into its slot. The live login is untouched unless it is that account, which then gets the new login too. `--claude-path PATH` overrides `prime.claudePath`. Falls back to printing the manual steps when claude can't be launched |
| `cc-swap remove <num\|email>` | Remove an account |
| `cc-swap disable <num\|email>` | Hold an account out of auto-rotation (keeps its login) |
| `cc-swap enable <num\|email>` | Return a disabled account to rotation |
| `cc-swap alias <num\|email> <name>` | Set a short alias, usable anywhere a number or email is accepted. `--unset` removes it; no arguments lists aliases |
| `cc-swap swap <a> <b>` | Exchange two accounts' slot numbers |
| `cc-swap move <a> <slot>` | Assign an account to a slot: an empty slot relocates it, an occupied one swaps the two |
| `cc-swap last-resort add\|remove\|list <account>` | Edit `maximize.lastResort` by email or alias (never by slot number, because slots move). `list` is the default |
| `cc-swap unclaimed [--purge ID]` | List stashed credential entries (slot and why they were stashed), or drop one |
| `cc-swap list` | Every account with 5h/7d usage, reset times and login deadline. `--token-status` adds source-labelled OAuth token diagnostics; `--json` for scripts |
| `cc-swap status` | The active account (`--json`) |
| `cc-swap export <path>` / `cc-swap import <path>` | Back up and restore accounts (see [Backup and migration](#backup-and-migration)) |
| `cc-swap import-usage <path>` | Adopt usage readings another machine took (see [Share usage readings](#share-usage-readings-between-machines)) |
| `cc-swap purge` | Remove all claude-swap data (refuses while a session-mode `claude` is running) |

### Switching

| Command | What it does |
|---|---|
| `cc-swap switch` | Rotate to the next account (skips disabled accounts and dead logins) |
| `cc-swap switch <num\|email\|alias>` | Switch to one account |
| `cc-swap switch --strategy best` | Switch to the account with the most 5h/7d quota left |
| `cc-swap switch --strategy next-available` | Rotate, skipping accounts at their limit |

`switch` flags: `--model NAMES` (with `--strategy`: also count these models' per-model weekly limits; defaults to `autoswitch.model`), `--force` (with a target: activate the stored credentials without backing up the current login first), `--allow-dead-login` (with a target: switch even to a dead or quarantined login, which is refused otherwise; the current login is still backed up first), `--json`.

### Automatic switching

| Command | What it does |
|---|---|
| `cc-swap auto` | Run the auto-switch engine in the foreground |
| `cc-swap auto off\|on\|status` | Stop or resume automatic switching, persistently (`--json`) |
| `cc-swap hold [DURATION \| until HH:MM \| off \| status]` | Stay on the active account (`--json`) |
| `cc-swap service install\|uninstall\|status` | Run the engine as a background service |
| `cc-swap prime [N …] [--dry-run]` | Open idle accounts' 5h windows now |
| `cc-swap prime verify [--live [NUM]] [--json]` | Re-check priming isolation after a Claude Code update |
| `cc-swap history [-n N] [--json]` | Recent account switches |
| `cc-swap notify test\|status [--json]` | Desktop notifications |

`cc-swap auto` flags:

| Flag | Meaning |
|---|---|
| `--once` | Evaluate once, maybe switch, and exit; the exit code is the outcome |
| `--json` | One JSON event per line on stdout |
| `--interval SECONDS` | Poll interval in loop mode (min 15; default 60) |
| `--threshold PCT` | `best`/`consume-first`: switch when the binding window reaches this (50–99.9; default 90) |
| `--cooldown SECONDS` | Minimum time between proactive switches (default 300) |
| `--model NAMES` | Also switch on these per-model weekly limits (`Fable`, `Fable,Opus`, or `all`) |
| `--include-api-key-accounts` / `--no-include-api-key-accounts` | Allow switching onto managed API-key accounts as a last resort (they bill per token; default excluded) |
| `--strategy {best,consume-first,maximize}` | Override `autoswitch.strategy` for this run |
| `--soft5h`, `--hard5h`, `--soft7d`, `--hard7d PCT` | `maximize` only: override the thresholds for this run (1–99.9; soft ≤ hard) |
| `--dry-run` | Evaluate and report, but never switch or write state |

Exit codes with `--once`: `0` switched, `1` error (network trouble, lock contention, ...), `2` nothing to do, `3` blocked (wanted to switch but no viable target, or all exhausted). Exit code `4` in any mode: another engine holds the engine lease. `--once --dry-run` needs no lease and always runs.

### Diagnostics

| Command | What it does |
|---|---|
| `cc-swap doctor [--json]` | Read-only check of this machine and every login (exit 0 fine, 1 warnings, 2 errors) |
| `cc-swap init [--apply] [--json]` | Onboarding and migration checklist (exit 0 when every step is ok, else 1) |
| `cc-swap why [--json] [--no-fallback]` | Why the engine did or didn't switch |
| `cc-swap repair-live [--yes]` | Repair a mixed live login (macOS): `~/.claude.json` names one account, the Keychain still holds a managed slot's token, and `~/.claude/.credentials.json` a newer login — a `/login` while the Keychain was locked, e.g. over SSH. Refuses while the Keychain is unreadable; checks with the token-owner lookup that the plaintext login is the named account's; after a confirmation (or `--yes`) re-checks under the switch's locks that the Keychain, the plaintext file and `~/.claude.json` are still exactly what it checked (a running Claude may have rotated the Keychain login meanwhile: then nothing is written), writes it into the Keychain and that account's slot — keeping MCP logins from both sides, the plaintext's winning — and removes the plaintext file. An account with no slot gets the Keychain only; run `cc-swap add` next |

### Session mode and directories

| Command | What it does |
|---|---|
| `cc-swap run <num\|email> [-- …]` | Run Claude Code as an account in this terminal only |
| `cc-swap run` | Run the current directory's mapped account |
| `cc-swap map <num\|email> [path]` / `cc-swap map` | Map a directory to an account / list mappings |
| `cc-swap unmap [path]` | Remove a mapping (defaults to the current directory) |

### Interface and maintenance

| Command | What it does |
|---|---|
| `cc-swap` / `cc-swap tui` | Full-screen TUI (Fleet under `maximize`) |
| `cc-swap watch` | TUI opened on the live watch page |
| `cc-swap menubar` | macOS menu bar app (`--install-service`, `--uninstall-service`, `--service-status`) |
| `cc-swap config` | Show or edit settings (see [Settings](#settings)) |
| `cc-swap upgrade` | Install the latest fork release (`--check`, `--force`) |
| `cc-swap --version` | Print the installed version |

## Settings

Settings live in `settings.json` in the backup root, in sections `autoswitch`, `ui`, `maximize`, `prime` and `notify`. `cc-swap config` reads and edits the file with validation, so you never have to find it or guess valid ranges:

```bash
cc-swap config                              # list effective settings ("(default)" = not set)
cc-swap config get autoswitch.threshold
cc-swap config set autoswitch.threshold 80  # validated: rejects out-of-range values loudly
cc-swap config set autoswitch.model Fable   # per-model switching; Fable,Opus for several
cc-swap config unset autoswitch.threshold   # back to the default
cc-swap config path                         # where settings.json lives
```

`cc-swap config --help` lists every key with its default. Hand-editing the file still works; `cc-swap config` is just a safer front door. `list` and `get` take `--json`. On load, an out-of-range value in the file is clamped to its range; `cc-swap config set` rejects it instead, and rejects a soft threshold above its hard ceiling.

### Upstream keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `autoswitch.threshold` | float 50–99.9 | 90 | `best`/`consume-first`: switch when the binding 5h/7d window reaches this % |
| `autoswitch.intervalSeconds` | float 15–3600 | 60 | Poll interval of the `cc-swap auto` loop, in seconds |
| `autoswitch.cooldownSeconds` | float 0–86400 | 300 | Minimum seconds between proactive switches |
| `autoswitch.hysteresisPct` | float 0–50 | 10 | A target must beat the active account by this many points |
| `autoswitch.includeApiKeyAccounts` | bool | false | Allow rotating onto managed API-key accounts (bill per token) |
| `autoswitch.unhealthyTicks` | int 1–100 | 3 | Consecutive failed polls before an account is unhealthy (and `maximize` fails over) |
| `autoswitch.model` | string | — | Also switch on these models' weekly limits (`Fable`, `Fable,Opus`, or `all`) |
| `ui.theme` | choice | auto | TUI colour theme: `dark`, `light`, or `auto` (follows the terminal background) |

### cc-swap keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `autoswitch.strategy` | choice | best | `best`, `consume-first`, or cc-swap's `maximize` |
| `maximize.soft5h` | float 1–99.9 | 50 | 5h soft threshold: at or above it, switch at the next idle moment |
| `maximize.hard5h` | float 1–99.9 | 95 | 5h hard ceiling: switch at once (must be ≥ soft5h) |
| `maximize.soft7d` | float 1–99.9 | 90 | 7d soft threshold |
| `maximize.hard7d` | float 1–99.9 | 98 | 7d hard ceiling (must be ≥ soft7d) |
| `maximize.landingMargin` | float 0–30 | 5 | A target must sit this far below both soft thresholds |
| `maximize.idleWindowMin` | int 3–60 | 10 | Minutes over which "idle" is judged, and over which the recent pace is measured |
| `maximize.idleMaxDeltaPct` | float 0–10 | 1 | Most 5h growth (percentage points) in that window that still counts as idle (the 7d allowance is fixed at 1 point) |
| `maximize.forceEtaMin` | int 0–60 | 10 | Switch at once if the recent pace reaches a hard ceiling within this many minutes (0 = off) |
| `maximize.resetWaitMin` | int 0–60 | 15 | Skip a hard or soft switch while the window that triggered it resets within this many minutes and the recent pace stays under 100% until 2 minutes past the reset (0 = off) |
| `maximize.pendingPollS` | int 180–600 | 180 | Active-account poll interval while waiting for idle (floor 180 s: the usage endpoint allows ~30 requests/hour per account, shared by every machine) |
| `maximize.rebalanceCooldownMin` | int 0–240 | 30 | Minimum minutes between rebalancing switches |
| `maximize.tieEpsilon` | float 0–2 | 0.1 | Scores this close count as a tie |
| `maximize.lastResort` | string | — | Last-resort accounts: emails or aliases, comma-separated |
| `maximize.planOverride` | string | — | Manual plan per account: `alice@example.com:20x,bob@example.com:5x` |
| `maximize.loginExpiryGuardMin` | int 0–1440 | 120 | A soft or rebalance switch never lands on an account whose login expires within this many minutes (an at-limit or hard fallback still may) |
| `maximize.preempt` | bool | true | Switch at an idle moment when the active 7d is on pace to pass `soft7d` before your next quiet time (trigger `preempt`) |
| `maximize.learnIdlePattern` | bool | true | Learn your usual busy and quiet times from the usage history |
| `maximize.preemptHorizonMaxH` | int 1–48 | 12 | Look at most this many hours ahead for a pre-emptive switch |
| `maximize.busyRebalanceGap` | float 0–5 | 0.5 | In a usually-busy time, rebalance only for a score gain at least this large; smaller ones wait for a quiet window |
| `maximize.learnedRide` | bool | true | Past a hard ceiling of 99 or more, use a learned share of the last point before switching (`ride`; see *Riding the last point*) |
| `maximize.rideWindows` | choice | 7d | The windows that ride: `7d`, `5h`, `5h,7d`, or `""` for none; any other window switches at its hard ceiling |
| `maximize.rideMaxMin` | int 0–120 | 30 | A ride lasts at most this many minutes after its arm time (0 = no ride) |
| `prime.enabled` | bool | false | Turn 5h priming on |
| `prime.model` | string | claude-haiku-4-5 | Model used for the priming request |
| `prime.jitterS` | string | 45-300 | Random delay after a reset before priming, in seconds, as `LO-HI` (HI at most 599) |
| `prime.maxAttempts` | int 1–5 | 2 | Attempts per 5h window |
| `prime.claudePath` | string | auto | `claude` executable (detected and saved by `cc-swap service install`) |
| `prime.autoVerify` | bool | true | After a Claude Code update, the engine runs the zero-cost `cc-swap prime verify` itself (see *After every Claude Code update*) |
| `notify.enabled` | bool | true | Desktop notifications from the engine (see [Desktop notifications](#desktop-notifications-cc-swap-notify)) |
| `notify.switch` | bool | true | Notify an account switch, with its trigger |
| `notify.relogin` | bool | true | Notify an account that needs a re-login |
| `notify.loginExpiring` | bool | true | Notify a login that ends within 24 hours (once a day per account) |
| `notify.primePaused` | bool | true | Notify priming paused after a Claude Code update, and the engine's automatic verify passing or failing |
| `notify.keychain` | bool | true | Notify a live login unreadable for over 15 minutes |
| `claude.settleS` | int 0–86400 | 600 | The engine never runs a `claude` binary changed less than this many seconds ago (see *Every `claude` cc-swap runs*); 0 turns the wait off |

The plan (5x/20x) comes from each account's stored credentials; an entry in `maximize.planOverride` wins over it. It matters only for breaking ties.

## The `maximize` strategy

Turn it on with `cc-swap config set autoswitch.strategy maximize`, or use `cc-swap auto --strategy maximize` for one run. It has separate soft and hard thresholds for the 5-hour and 7-day windows. It spends the accounts whose weekly quota would otherwise expire unused first, switches at an idle moment once a soft threshold is crossed, and switches immediately at a hard ceiling. It waits out a window that is about to reset instead of switching, and learns when you are usually quiet so a move it can see coming happens at an idle moment rather than in the middle of your work.

cc-swap does not manage extra usage (pay-as-you-go beyond the plan). If you never want it, turn it off in your claude.ai account settings.

**Tiers.** Accounts held out with `cc-swap disable` are *excluded*: they are never a target and never primed, although a manual `cc-swap switch` still works. Accounts listed in `maximize.lastResort` are *last resort*: they are used only when no normal account can take you. `cc-swap last-resort add|remove|list <account>` edits that list by email or alias, never by slot number, because slots move. Every other account is *normal*.

**Where it may land.** A target must sit below each soft threshold by `maximize.landingMargin`. It must not be quarantined or an API-key account, and its usage must be known. A target whose 5h window is off counts as 0%. When a switch is forced (`at-limit` or `hard`) and no account can land, the engine falls back to accounts under both hard ceilings, then to the last-resort pool; an account whose login has passed its deadline is never a target.

**Which account first.** `score = (100 − 7d used %) ÷ (days until the 7d reset × 100/7)`, with the days floored at one hour (an unknown reset counts as 7 days; an unknown 7d usage ranks last and cannot land). A score above 1 means more weekly quota is left than an even pace would use before the reset; that quota would otherwise expire. Higher scores go first: an account with 30% left and a reset in 12 hours (score 4.2) beats one with 90% left and six days to go (score 1.05). Scores within `maximize.tieEpsilon` tie (a tie group is anchored on its highest score, so ties never chain further than that). Ties go to 20x plans first, then to the 5h window that resets soonest (a window that is off goes last), then to the lower slot.

**When it switches** (the first match wins):

| Trigger | When | Waits for idle |
|---|---|---|
| `at-limit` | the active 5h or 7d window is at 100% | no |
| `hard` | 5h ≥ `hard5h` or 7d ≥ `hard7d`, or the recent pace (over `idleWindowMin`, default 10 minutes) reaches a hard ceiling within `forceEtaMin` (a ceiling of 99 or more on a window in `rideWindows` rides the last point first; see *Riding the last point* below) | no |
| `soft` | 5h ≥ `soft5h` or 7d ≥ `soft7d` | yes |
| `preempt` | the active 7d is on pace to pass `soft7d` before your next quiet time and another account's is not (`maximize.preempt`; see *Switching while you are idle* below; at most once per `rebalanceCooldownMin`) | yes |
| `rebalance` | a better-scored account exists, or the active one is excluded / last resort (at most once per `rebalanceCooldownMin`) | yes |
| `failover` | the active account's usage could not be read `autoswitch.unhealthyTicks` times in a row | no |

*Idle* means two usage readings at least `idleWindowMin` apart, with at most `idleMaxDeltaPct` growth in the 5h window and at most 1 point in the 7d window. A gap in the readings longer than `idleWindowMin`, or a newest reading older than that, counts as unknown, never as idle. The recent pace is used only when the newest reading is within `idleWindowMin` of now and the readings span more than 2 minutes. While it waits, the active account is polled every `pendingPollS` seconds. Usage from other machines on the same account counts too: there is no coordination between machines, and cc-swap trusts only what the server reports. The `rebalanceCooldownMin` cooldown runs from the later of the last engine switch and the last change of the active account, so a manual switch restarts it.

**Staying past a hard mark.** When a `hard` trigger fires but no account under the hard ceilings has more room on that window than the active one, maximize stays (`hard-stay`) rather than move somewhere that would force a move straight back. At 100% the `at-limit` switch moves you to whatever has quota left.

**Waiting out a reset.** A switch makes Claude Code re-read the whole context on the new account, and a window that is about to reset clears the reason to switch. So when the window behind a `hard` or `soft` switch resets within `maximize.resetWaitMin` minutes (default 15) and the recent pace will not reach 100% until at least 2 minutes after that, maximize holds (`reset-wait`), polling the active account every 60 s in the last 15 minutes before the reset. The pace is projected from the last reading and counted from now, so an older reading leaves less room. With no pace measured yet, it waits only while that window is under its hard ceiling, and a window past its hard ceiling is never waited out while the active account is backing off after a 429 (it could not be polled every 60 s). At 100% the `at-limit` switch still happens at once, a trigger on the other window still switches, and `rebalance` never waits. Fleet's top line reads `Auto ON · using #1 main · 5h 96% — resets in 8m, waiting it out (switches at once if it hits 100%)`, counting the minutes down.

**Riding the last point.** The usage API reports whole percents, rounded down: `99` means anywhere from 99.0 to 99.99, and at 100% Claude Code's next request fails and the running turn stops. A hard ceiling of 99 therefore switches with up to a whole point unused. With `maximize.learnedRide` (on by default), when a window listed in `maximize.rideWindows` (default `7d`) first reads its hard ceiling, and that ceiling is 99 or more, maximize keeps using the account a little longer (`ride`) and switches at *arm time + q × T1 − 90 s*, at most `maximize.rideMaxMin` minutes (default 30) after the arm time. Readings come a poll apart, so the window may have crossed its ceiling right after the reading before the first one at it: the *arm time* is that previous reading (when it is at most 30 minutes older), else 5 minutes before the first reading at the ceiling. Each account keeps its arm time until that window resets, so switching away mid-ride and back does not start the ride over. *T1* is how long one point takes on that window, fixed at the arm time: the shorter of the recent pace (over `idleWindowMin`) and the median of the last three whole-point steps the engine timed on that account in the last 6 hours, whichever are known; with neither, it switches at once as before. A step is timed only when the account was in use all the way to it: a stretch longer than `idleWindowMin` with no rise on either window (a night, a lunch break) leaves the next step untimed, and a 7d reset, a gap of more than 30 minutes in the readings, or time parked while another account was active clears what was timed. *q* is a share of that point, learned per window: it starts at 0.3 and stays between 0.05 and 0.9; a ride that ends in its switch before 100% raises it by 0.05 (unless `rideMaxMin` ended it before its learned share: that says nothing about q), and a ride that reads 100% first halves it. The 90 s are one 60 s poll plus 30 s. During the ride the account is polled every 60 s over the last 15 minutes before its switch (`pendingPollS` before that), and it switches at once the moment you pause (idle is the cheapest moment to switch; that teaches nothing), or at 100% (`at-limit`). A ride never starts while the active account is backing off after a 429 (it could not be polled every 60 s), a ride that the window's reset would end first is a `reset-wait`, a `hard` trigger on a window that is not listed (5h by default: it switches at its ceiling, or earlier by `forceEtaMin`, exactly as before) still switches at once, and an account hold does not stop it: the ride *is* the hard switch, only later. Fleet's top line reads `Auto ON · using #5 side · 7d 99% — riding to the limit, switching in ~2m (learned) or at your next pause`, counting the minutes down (`capped` instead of `learned` when `rideMaxMin` ends it). `cc-swap why` and `cc-swap doctor` show what has been learned: `learned ride: 5h off (rideWindows; learned 0.30) · 7d rides 0.35 of the last point (3 ok, 1 hit)`. Set `maximize.learnedRide` to false, or `maximize.rideWindows` to `""`, to switch at the hard ceiling.

**Switching while you are idle.** The engine keeps a small usage history (`usage_history.jsonl` in the backup root: each account's hourly 5h/7d percentages for 8 days, and for 14 days whether the active account's 5h rose in each 15-minute slot). It records only readings it already has, so the poll budget is unchanged. From that it learns when you are usually busy, weekdays and weekends apart, once it has 3 days of data; a *quiet window* is an hour or more of slots that were busy less than 20% of the time. With `maximize.preempt`, if the active 7d is on pace to pass `soft7d` before your next quiet window (at most `preemptHorizonMaxH` hours away, 4 hours while nothing is learned yet), and another account would not, it switches at an idle moment now (trigger `preempt`, after `rebalanceCooldownMin`) instead of being forced to later. In a usually-busy time, a rebalance gaining less than `busyRebalanceGap` waits for a quiet window that starts within 6 hours, and a rebalance never lands on an account whose own 7d would pass `soft7d` within that same horizon (preempt would only move you off it again). Neither ever overrides `at-limit`, `hard`, `soft` or `reset-wait`. `cc-swap why` and `cc-swap doctor` show what has been learned, and so do Fleet's help (`?`) and its Swap strategy screen: `idle pattern: 9 days learned · next quiet window 23:00–07:30`.

`cc-swap auto --once --dry-run` prints each account's tier, score, landing eligibility and idle state, plus the decision and its reason. It needs no engine lease, so it works while the service runs.

### Changing thresholds while it runs

The four thresholds (`soft5h`, `hard5h`, `soft7d`, `hard7d`) can change at any time. A running engine, including the service, picks up a change on its next tick without a restart:

- **Persistent**: `cc-swap config set maximize.soft5h 60`. The range and soft ≤ hard are both validated.
- **One run**: `cc-swap auto --soft5h 60 --hard5h 95 --soft7d 90 --hard7d 98`. Flags override the file.
- **TUI**: on the Fleet home screen open the menu (`m`) and press `s` (Swap strategy) to edit the thresholds, the reset wait, the idle-pattern knobs and priming with a live preview (see [Fleet](#fleet-the-tui-home-for-maximize)). Or open the auto screen (`g` from the classic dashboard; `e` or `g` from Fleet) and press `t` to select 5h soft. Press `t` again to move to 5h hard, 7d soft and 7d hard. `←`/`→` move the selected value by 1 (clamped to its range; a soft mark never passes its hard one), `enter` saves to `settings.json`, and `esc` discards. The 5h and 7d bars show the soft threshold as a yellow tick and the hard ceiling as a red one. Saving works even when the screen is only a viewer of the service's engine.
- The engine checks the modification time of `settings.json` every tick. If the new values fail validation, it keeps the old ones and logs a configuration warning.

## The `best` and `consume-first` strategies

These are upstream's strategies, still available (`autoswitch.strategy`, or `--strategy`). When the active account's 5-hour or 7-day window reaches `autoswitch.threshold` (default 90%), the engine switches before you hit the limit, and it is safe to run while Claude Code is working:

```bash
cc-swap auto                     # foreground loop, polls every 60s
cc-swap auto --threshold 80      # switch earlier
cc-swap auto --model Fable       # also switch when the Fable weekly limit is hit
cc-swap auto --once              # single check-and-switch, for cron/scripts
cc-swap auto --dry-run           # log what it would do, never switch
cc-swap auto --strategy consume-first   # burn the soonest-resetting account first
```

- `best` (the default) stays put until the active account nears its limit, then moves to the account with the most quota left. `consume-first` proactively keeps you on the account whose **weekly window resets soonest** (use it or lose it), switching to a sooner-resetting account with room to spare even below the threshold, so perishable weekly quota isn't wasted.
- Switches take the same credential locks Claude Code uses, so a swap never collides with a token refresh.
- A cooldown (`autoswitch.cooldownSeconds`, default 5 minutes) and a hysteresis margin (`autoswitch.hysteresisPct`) stop it flip-flopping near the threshold: a proactive switch only lands on an account that is below the threshold *and* better than the current one by the margin. A candidate that clears the margin is always taken, but two accounts hovering at the line never ping-pong. When every account is exhausted it keeps checking on a bounded slow cadence, waking sooner for an imminent reset.
- Usage polling is adaptive (a couple of accounts per check, busy alternates watched more closely, exhausted ones checked about every ten minutes, or slower after 429s), so API traffic stays flat however many accounts you manage.
- It fails safe: if a usage check errors it keeps trusting the last-known numbers while retries back off, and an expired token on an idle machine makes it hold rather than fail over (Claude Code refreshes the token on your next message).
- An account whose refresh token has died is quarantined and reported until you either log in with it and re-run `cc-swap add --slot N`, or replace its stored credentials from a known-good export: a plain `cc-swap import backup.cswap` replaces dead-token slots on its own (`--force` is still required to replace other existing accounts; a stale export can carry an already-superseded token). API-key accounts are never rotated onto unless you pass `--include-api-key-accounts`.
- To hold an account out of rotation yourself (a work account you don't want touched, one you're resting), run `cc-swap disable <num|email>`; `cc-swap enable <num|email>` puts it back. Disabled accounts are skipped by auto-switch, bare `cc-swap switch`, and the `best` / `next-available` strategies, but stay fully managed and remain a valid explicit `cc-swap switch <num|email>` target. They show a `(disabled)` marker in `cc-swap list`, in the classic TUI and in the menu bar, both of which also toggle the state in place (TUI: menu → *Disable / enable account…*; menu bar: *Disable / enable account*). Under `maximize` the same accounts are *excluded*, and Fleet toggles them with `x`.
- By default only the account-wide 5h/7d windows drive switching. If you work on one model and hit its **weekly per-model limit** first (for example Fable), add `--model Fable` (or `cc-swap config set autoswitch.model Fable`) to fold that model's window into the decision, so it switches off an account whose model quota is spent even while its 5h/7d windows still have room. Model names are Anthropic's own per-model `display_name`s, matched case-insensitively; the exact strings for your accounts are the per-model rows in `cc-swap list` (for example a line reading `Fable: 100%`).

For cron or systemd timers, `--once` reports the outcome in its exit code (see [Commands](#automatic-switching)) and `--json` emits one JSON event per line:

```bash
*/5 * * * * cc-swap auto --once --json >> ~/.cswap-auto.log 2>&1
```

Do not combine a cron job like this with the cc-swap service: only one engine runs per machine, and the second exits with code `4`.

## 5h window priming

A 5-hour window starts with an account's first request: the window resets 5 hours after that request, rounded down to 10 minutes. An idle account reports no window at all (`resets_at` is empty), and usage polling does not start one. So an account you have not touched since its last reset starts its 5 hours only when you switch to it.

With `prime.enabled` on, cc-swap sends each idle account one tiny request ("Reply OK", using `prime.model`) 45–300 seconds after its window resets (`prime.jitterS`). The window then runs from that moment. If you come to the account later in those 5 hours, its quota is still there, and it resets again sooner, so you can use up to twice the 5h quota within the same five hours. Priming never touches the active account or excluded accounts.

Each priming run is built so it cannot disturb your login:

- It checks the account's usage first (a reading at most 60 seconds old), and skips the account if a window is already running (opened by another machine, or by you). It also skips API-key, quarantined and login-expired accounts, accounts whose 7d is exhausted, accounts with a live Claude session (re-checked after 10 minutes), and accounts whose usage is unknown.
- It runs the official `claude` CLI once: `claude -p --model <prime.model> --safe-mode --tools "" --no-session-persistence --max-turns 1 --output-format json "Reply OK"`, with a 90-second timeout. If the model is not found, the same attempt is retried with the `haiku` alias. The run uses an isolated config directory (`<backup root>/prime-profile`) and gets only the account's **access token**, in `CLAUDE_CODE_OAUTH_TOKEN`. It never gets the refresh token, so the account's token chain cannot fork. `ANTHROPIC_*` credentials, the API base URL, third-party provider variables, other `CLAUDE*` token and file-descriptor variables, `CLAUDE_CONFIG_DIR` and Claude Code's nesting variables are removed. On macOS, any Keychain item the run creates for that profile is deleted afterwards.
- Success is judged by the window's reset time appearing, because utilization often stays at 0%. An attempt that could not be confirmed, or that failed authentication, is retried at once; other failures (a timeout, an error exit) wait and retry on the normal jitter schedule. `prime.maxAttempts` caps the attempts per window; after that, priming waits for the next reset. An account the usage endpoint reports as rate limited is not primed until its 7d reset (or for an hour if that is unknown). If `claude` cannot be found, priming switches itself off with a warning.

Priming finds `claude` at `prime.claudePath`, then `~/.local/bin/claude`; it does not search `PATH`. `cc-swap service install` finds `claude` in your shell (`prime.claudePath`, then `PATH`, then `~/.local/bin/claude`) and saves it as `prime.claudePath`, because launchd and systemd start the service without your shell's PATH. Set it yourself (or pass `cc-swap service install --claude-path PATH`) if `claude` lives somewhere unusual.

`cc-swap prime [N …] [--dry-run]` primes on demand (even while `prime.enabled` is false), or shows what it would do. Every launch needs a usage reading from the last minute; an account it cannot prime right now (for example while the usage endpoint is throttling it) gets a `#N  not primed (reason)` line and exit status 1, so run it again once the wait it names has passed.

With several machines, each one primes on its own schedule with its own random jitter, and an account whose window another machine already opened is skipped.

> **Terms of service.** Anthropic's consumer terms treat subscription OAuth access as being for ordinary use and allow Anthropic to act without notice. Priming makes one automated request per idle account after every 5-hour reset. With several accounts that is a steady, machine-like pattern, even though it goes through the official `claude` CLI. The jitter blurs the timing but does not remove the risk. Priming is **off by default**. Turn it on with `cc-swap config set prime.enabled true` only if you accept that risk for your accounts.

### After every Claude Code update: `cc-swap prime verify`

Priming depends on how the `claude` CLI handles `CLAUDE_CODE_OAUTH_TOKEN` and its Keychain fallback, and that can change between Claude Code versions. So priming remembers the Claude Code version its isolation was last verified with (`verifiedClaudeVersion` in `<backup root>/prime_verify.json`) and **pauses itself** as soon as the installed `claude` reports a different one. Fleet's attention line then reads `! priming paused: claude 2.1.3 -> 2.1.4 (the engine re-verifies it; or cc-swap prime verify)` (`… (cc-swap prime verify)` with `prime.autoVerify` off), the engine log warns once, `cc-swap prime` fails with the reason, and `cc-swap doctor` warns. With `prime.autoVerify` on (the default) the engine lifts the pause itself; see below. To verify by hand, run:

```bash
cc-swap prime verify            # zero-cost checks; records the version when they pass
cc-swap prime verify --live     # also one real prime of an idle account (or --live 3)
cc-swap prime verify --json     # the same report for scripts
```

It replaces the manual checklist earlier releases asked for. Without `--live` it costs nothing: it runs `claude` once in a throwaway profile with an invalid token and checks that the run fails with a clean 401, leaves no Keychain item and no `.credentials.json` behind, and leaves the active login unchanged: the Keychain item's attributes (never its secret), `~/.claude/.credentials.json` and the account in `~/.claude.json` are compared by hash before and after. `--live` then primes one idle account (one whose 5h window is off) for real and checks the same things again. When every check passes it records the version and priming resumes on the engine's next tick, with no restart; otherwise it exits 1, records the failure and removes the earlier verified version, so priming stays paused even if an older version had passed. If a check fails, keep priming off (`cc-swap config set prime.enabled false`) and open an issue that includes your `claude --version`.

**Automatic verify (`prime.autoVerify`, default on).** When the pause is only a version change (the installed `claude` differs from the verified one), the engine runs the same zero-cost checks itself at the end of a tick, where it would otherwise prime, after any switch — never `--live`, and never when `claude --version` cannot be read. It takes a lock (`.prime_verify.lock` in the backup root) that `cc-swap prime verify` also takes, so two verifies never run at once. If every check passes it records the version (`verifiedBy: "engine auto-verify"`), priming resumes on the next tick, and you get a *priming resumed* notification. A check that fails because of isolation (an accepted invalid token, a 429 instead of a 401 — an invalid token is refused before any rate limit, so `claude` used another credential — a Keychain item or `.credentials.json` left behind, a changed active login) records a failed verify exactly as `prime verify` does and notifies you: priming stays paused until a manual `cc-swap prime verify` passes, and the engine never tries that version again, not even after a later version passed and `claude` was rolled back to it (`failedVersions` in `prime_verify.json`). A failure that may be passing (no answer in time, `claude` not starting, a network error, a Keychain that cannot be read — locked, or `security` failing or timing out) is retried 30 minutes later, at most 3 tries per version; the last one records a failed verify. A Keychain that cannot be read never counts as unchanged, in `prime verify` either (there it fails the run). A newer `claude` after a failed one gets its own automatic verify. With `prime.autoVerify` false, nothing is verified automatically.

The engine reads `claude --version` only when the executable changed (it caches the answer by the file's identity). An install that never ran `prime verify` takes the first prime the usage endpoint confirms as its baseline.

cc-swap never updates or replaces Claude Code: Claude Code's own updater does (in the background, or when you run `claude update`), and the guard notices the changed `claude` file on its own. A `claudeVersion` key an older cc-swap wrote into `autoswitch_state.json` is ignored.

### Every `claude` cc-swap runs

cc-swap starts `claude` for priming, `prime verify` (`--version` and the invalid-token probe), `cc-swap login` (`auth login` and its `--help` probe), `cc-swap doctor` (`--version`) and `cswap run` (its `auth status` probe and the session itself). Each run goes through one guard (`maximize/claude_exec.py`):

- **Auto-updater off.** Every child except the `cswap run` session itself gets `DISABLE_AUTOUPDATER=1`: priming starts several `claude` right after each 5h reset, and each would otherwise run Claude Code's background updater against the shared install.
- **Audit log.** One line per run in the cc-swap log and in `claude-exec.jsonl` in the backup root (moved to `claude-exec.jsonl.1` past 2 MB): when, which feature, the arguments (emails and tokens masked; never the environment), the `claude` path and the real file it resolves to, that file's inode, size, mtime, ctime and birthtime, the symlink's own mtime, the file's age, pid, exit code, signal and duration.
- **Binary watch.** The first time a new `claude` file is seen (the engine looks every tick while priming is on), a *claude binary changed* line records the old and new file, and on macOS `codesign -dv` (Identifier, CDHash, TeamIdentifier, Timestamp), `codesign --verify` and the file's xattr names. If the same file (same inode) is rewritten after cc-swap ran it, the log warns `claude binary at … was rewritten in place after it had been executed (by cc-swap at …)`, and `cc-swap doctor` repeats it for a week; a new file at the path (an update's rename, the fix below, an npm install) is not a rewrite. Each run's audit line also carries the file's xattr names and ctime before and after it, and a change during a cc-swap run is a warning with both values.
- **Settle delay.** The engine does not run a `claude` whose file (or symlink) changed less than `claude.settleS` (600 s) ago: priming, its version check and its automatic verify wait for a later tick, and Fleet, `cc-swap doctor` and `cc-swap why` say `priming paused: waiting for the claude update to settle (…s left)`. A command you type (`cc-swap prime verify`, `cc-swap prime`, `cc-swap login`) runs it anyway and prints a warning — you asked for that run, and waiting ten minutes would only stall you; the audit line notes the override. `cswap run` runs it too and only logs the warning, so the session starts on a clean terminal. `cc-swap doctor` skips its `--version` instead.
- **Killed by the OS.** A `claude` that ends with SIGKILL (exit -9, or 137 through a wrapper) is recorded as killed by the OS — never as a failed verify. Priming pauses, you get one notification per `claude` file, `claude-kill-<time>.txt` in the backup root keeps the diagnostics (the run, `codesign`, xattr names and, on macOS, two minutes of `log show` for the pid, `claude`, AMFI and code signing), and `cc-swap doctor` shows an error with the fix: `cp -p <real> <real>.tmp && mv <real>.tmp <real>` (cc-swap never runs it for you). The engine does not run that file again; the mark clears by itself once the `claude` path resolves to another file (a new Claude Code version lives at a new path, and the fix gives the file a new inode) or any run of it succeeds, and nothing reports a mark whose path no longer resolves to the killed file. `cc-swap doctor` runs its own `--version` record-only: one audit line, never a mark, a pause or a notification.
- **Code-signing kill watcher (macOS).** While an engine runs (not a dry run), it keeps one `log stream --style ndjson` child filtered to code-signature messages (`code signature` anywhere, the kernel's `CODESIGNING`, `Code Signature Invalid`) and appends the kernel's lines and any line naming `claude` — time, pid, process, message; other apps' code-signing chatter is dropped — to `codesign-events.jsonl` in the backup root (moved to `.1` past 2 MB); every comparison uses the time the line itself carries (`2026-10-04 22:24:19.003000+0900`), not when it was read. It ends itself every hour (`--timeout 1h`) and is restarted at once, so one an engine killed outright leaves behind does not outlive it by long; if it dies otherwise it is restarted after 30 s, doubling up to an hour. The tick only polls it, the crash-report scan runs on its own thread, and the engine stops the child on exit. The child also asks for AppleSystemPolicy's `ASP: Unable to apply provenance sandbox: <error>, <pid>, <path>` lines and keeps only those naming `~/.local/share/claude/versions` (other CLIs, `codex` included, get them too). A kernel `load code signature error … for file "<version>"` naming the current `claude` file is a kill of a `claude` cc-swap did not launch (cc-swap's own launches see their SIGKILL themselves; every one is noted in flight — pid and start — until its end is recorded, so a kill whose ASP line names that pid, or a line without a pid while cc-swap's own run of that file started moments before, is left to that run; `cswap run`, which execs `claude` in place, is marked killed from it): it is **evidence, never a pause** — counted per `claude` file in `claude_exec_state.json` (`external`: how many, first and last, the launching app from the crash reports, the provenance line that came just before it) and logged as `external-kill` in `claude-exec.jsonl`. A minute after the latest such kill (at most 5 minutes after the first one not yet checked, and at most once per 10 minutes per file) the engine runs its own `claude --version` through the same guard (`external-kill probe`; not while the file is still settling; a probe still `running` after 95 s belonged to an engine that stopped and may run again). Exit 0 records a successful run, so the old evidence can never mark the file again; `cc-swap doctor` then warns (for a day, then an info line, for a week) "macOS killed claude <version> launched by <app> N times (first …, last …); cc-swap's own launches work", with the provenance hint when an ASP line came with the kills (a problem between macOS and that app: quit and reopen it). A probe the OS kills takes the killed-by-the-OS path above (priming pauses, one notification, the error with the fix). Only the kernel's lines count; other daemons' lines are kept as evidence only. A killed mark 0.5.3 set from such a kill (caller `log stream` or `crash report`) is read as this evidence after an upgrade: priming is not paused, and the engine probes it at once — unless 0.5.3 later counted a kill of one of its own launches on it (`lastCaller`), which keeps the pause. A bare file name counts only when it is a version number (a native install); any other `claude` must be named by its full path. Evidence from before a successful run of that file (a crash report, a line) never marks it again, across engine restarts too. On start and every hour the engine also reads the last week's crash reports in `~/Library/Logs/DiagnosticReports` (`*.ips` whose process is under `~/.local/share/claude/versions` and was terminated for `Code Signature Invalid`; macOS anonymizes the path to `/Users/USER/*/<file>`, so then the file name must be one in that directory or the current `claude`'s): new ones go to `codesign-events.jsonl`, and a kill of the current `claude` file (after it was written) is counted once as the evidence above, with its launching app — unless its pid is a `claude` cc-swap ran (`claude-exec.jsonl`), which marks the file killed by the OS as before. `cc-swap doctor` warns about those reports per version — how many, the first and last kill, the app that launched the killed process (the report's `parentProc` / `responsibleProc`), and the latest `claude` cc-swap launched before the first one (from `claude-exec.jsonl`), so a launch that came just before shows.

## Always-on service

```bash
cc-swap service install     # install (or refresh) and start it; `cc-swap upgrade` re-runs it for you
cc-swap service status      # installed? running? pid?
cc-swap service uninstall   # stop it and remove it
```

The service runs plain `cc-swap auto`, which reads its strategy from the settings.

- **macOS**: a LaunchAgent (`~/Library/LaunchAgents/com.wonjun-lab.cc-swap.plist`) that starts at login and restarts after a crash. Logs go to `~/Library/Logs/cc-swap/` (`auto.log`, `auto.err.log`). While another engine holds the lease `auto.err.log` grows by about 370 KiB per day, because the service logs the refusal once a minute, so the service rotates both files itself: at startup and at most once an hour, a file over 10 MiB is copied to `<name>.1` (three generations, `.1` to `.3`) and truncated in place. A terminal `cc-swap auto` does not rotate anything. On Linux the output goes to the journal, which rotates itself.
- **Linux**: a systemd user unit (`~/.config/systemd/user/cc-swap.service`, `Restart=on-failure`) that is enabled and started for you. Read its logs with `journalctl --user -u cc-swap -f`. To keep it running after you log out, enable lingering once with `loginctl enable-linger $USER`; `install` tells you when lingering is off.
- **Windows**: not supported. Run `cc-swap auto` in a terminal instead.

`install` forwards `CLAUDE_CONFIG_DIR` and `CLAUDE_SECURESTORAGE_CONFIG_DIR` from the shell you run it in to the service, and prints which ones it forwarded, so a custom profile is read by the service too. The refresh after `cc-swap upgrade` instead keeps the profile variables already in the installed service file (`--reuse-installed-env`), whatever the upgrading shell has set. `--claude-path PATH` sets the `claude` used for priming (saved as `prime.claudePath`). If launchd boots the old service out but cannot start the new one, `install` says "the service is now STOPPED - run: cc-swap service install" and exits 1. It refuses to install while `CLAUDE_CONFIG_DIR` points at a `cc-swap run` session profile; run it from a terminal outside the session. The service file sets `CC_SWAP_SERVICE=1` for itself; that marker turns on the log rotation above and the exit-75 restart below.

**An unreadable login holds the engine.** If the live credential cannot be read cleanly (for example, the macOS Keychain answers `rc=36` for a few minutes after a `/login`, or only the plaintext fallback is readable), the engine logs `Keychain unreadable; holding` once and makes no switch for any trigger until a read succeeds. These ticks do not count toward failover. Switching then would overwrite a login cc-swap cannot see, possibly one that has not been backed up yet. Once the Keychain answers again (re-checked every engine tick, default 60 s), switching resumes on its own. A hold that lasts more than 15 minutes logs what to do every 15 minutes (unlock the login keychain, or restart cc-swap) and sends a desktop notification (at most every 2 hours); the service exits with code 75 at that point so launchd or systemd restarts it. A terminal `cc-swap auto` keeps running and keeps logging.

**A new login on a dead active slot is adopted.** When the active slot's stored login is dead (quarantined, `re-login needed`, or `login expired`) and you have just run `/login` as that same account, the engine backs the new login up into the slot before doing anything else. It checks the config identity (email, organization, account id) and the token's own identity, as `cc-swap add` does. It then lifts the quarantine and logs `adopted new login for #N`. Until the backup is written, the engine makes no switch (reason `new-login-not-backed-up`). A dry run never adopts. A live login that belongs to no slot, or to a different account than the slot's, holds the engine with the warning `unmanaged login, run cc-swap add`.

**One engine per machine.** Whatever runs the engine (the service, a terminal `cc-swap auto`, the TUI's auto screen, or the menu bar's auto-switch) holds a lock, `<backup root>/.engine.lock`, for as long as it runs. The OS frees the lock when the process exits, even after a crash. While another process holds it, `cc-swap auto` refuses to start and exits with code `4`, and the TUI's auto screen (badge **VIEWER**) and the menu bar only show what the running engine is doing. `cc-swap auto --once --dry-run` needs no lock and always works. If another engine held the lease when the service started, the service retries every minute and takes over once that engine stops (launchd and systemd restart it 60 seconds after its exit 4). To run an engine in a terminal instead, run `cc-swap service uninstall` first.

## Turning automatic switching off: `cc-swap auto off`

```bash
cc-swap auto off           # stop switching and priming, until you turn it back on
cc-swap auto on            # resume
cc-swap auto status        # on or off, who turned it off and when (--json for scripts)
```

`off` is persistent and applies to whichever engine runs (the service, a terminal `cc-swap auto`, the TUI or the menu bar): it is stored as `autoOff` in `autoswitch_state.json` and mirrored in its own file, `auto_off.json` in the backup root, so a damaged state file cannot switch it back on (the file is authoritative, and an unreadable or damaged `auto_off.json` reads as off; `cc-swap auto on` removes it), survives restarts, and is picked up on the next tick without a restart. The engine keeps polling and deciding (Fleet's top line reads `Auto OFF — nothing switches automatically`, and `cc-swap why` says what it *would* do; the engine log says `no switch: auto-off` at most once an hour), but it never switches and never primes; manual switches still work. In Fleet, the menu's `o` (`m` → `o`, or `o` in Mode) turns it off and on; turning it off asks first (`y` or `enter` confirms, `n` or `esc` keeps it on), turning it on does not.

## Staying on one account: `cc-swap hold`

A switch makes Claude Code re-read the whole context on the new account. In the middle of a long task that costs more than staying on an account that is a little fuller, so you can pin the active account for a while:

```bash
cc-swap hold 2h            # stay on the active account for two hours (also 90m, 2h30m)
cc-swap hold until 23:00   # until 23:00 local time, the next one (tomorrow once it has passed)
cc-swap hold off           # lift it now
cc-swap hold status        # what is held and until when (also: no argument; --json for scripts)
```

While a hold lasts and its account is still the active one, maximize skips its `soft`, `preempt` and `rebalance` moves; `cc-swap why` and the engine log name the reason code `hold`. Safety always wins: `hard` (a hard mark reached, or reached within `forceEtaMin` at the recent pace), `at-limit` (100%) and `reset-wait` behave exactly as without a hold, and so does a learned ride. A hold lasts at most 24 hours and ends by itself at its end time; on a daylight-saving day `until` can name a time more than 24 hours away (a 25-hour day, or a time the clocks skip), and the hold then says so and ends at 24 hours. Any change of the active account ends it too: a manual switch, a `/login` outside cc-swap, or a forced switch.

The hold is stored in its own file, `hold.json` in the backup root, and mirrored as `accountHold` in `autoswitch_state.json`. Whichever engine runs (the service, a terminal `cc-swap auto`, the TUI or the menu bar) honours it on its next tick, and clears it once it no longer applies. In Fleet, `h` opens a small picker: `h` one hour (so `h h` holds an hour), `t` two hours, `f` four hours, `u` until a time, `o` off (labelled "no hold now" when nothing is held). Typing a digit in the picker starts the time instead (`12:00` then `enter`), so it never sets an hour's hold by accident. The top line then reads `Holding #1 until 15:30 (2h left) — only hard 98%/100% will move you`. `cc-swap why`, `cc-swap doctor` (as `info`) and `cc-swap auto status` show the hold too.

## Switch history: `cc-swap history`

Every account switch on this machine is appended to `<backup root>/switches.jsonl`, whoever makes it: the engine (with its trigger: `hard`, `soft`, `rebalance`, …), `cc-swap switch`, the TUI, the menu bar, or a Fleet re-login switching back. A live login that changed outside cc-swap (a `/login` inside a Claude Code session) is recorded too, as `external`. Entries hold slot numbers, the host and versions, never an email or a token. The file is private (0600) and rotates at 1 MiB into three generations.

```bash
cc-swap history            # the last 20 switches: when, #from -> #to, trigger, who
cc-swap history -n 0       # all of them
cc-swap history --json     # for scripts
```

When the live login is not where the last recorded switch went, it says so. In Fleet, the menu's `v` (*View switch history*) shows the newest 50 entries.

## Desktop notifications: `cc-swap notify`

The engine tells you about what matters while you are not looking at the TUI:

| Event | When | Setting |
|---|---|---|
| switch | the engine switched accounts, with its trigger (`switched to #2 side` · `from #1 main · hard: #1 5h 96% >= hard 95%`) | `notify.switch` |
| re-login | an account needs a re-login (its refresh token is dead, or its login passed its deadline); once a day per account | `notify.relogin` |
| login expiring | a login ends within 24 hours; once a day per account | `notify.loginExpiring` |
| priming paused | priming paused after a Claude Code update, until `cc-swap prime verify` passes (with `prime.autoVerify`, only once the engine's own verify failed); once a day | `notify.primePaused` |
| priming resumed | the engine's own verify passed after a Claude Code update (`prime.autoVerify`) | `notify.primePaused` |
| Keychain | the live login has been unreadable for over 15 minutes, so nothing switches; at most every 2 hours | `notify.keychain` |
| claude killed | the OS killed a `claude` cc-swap ran, at launch (SIGKILL); once per `claude` file, from any cc-swap process | `notify.enabled` |

They come from whichever engine runs (the service, a terminal `cc-swap auto`, the TUI or the menu bar), never from a dry run. macOS shows them with `osascript` (`display notification`); Linux with `notify-send` when it is installed (Debian and Ubuntu: `libnotify-bin`); on any other system nothing is sent. A notification names accounts by slot number and short name (the alias, else the part of the address before the `@`, as in Fleet), never an email or a token. They are deduplicated and rate-limited through `notify_state.json` in the backup root: the same notification is not repeated within its interval (30 seconds for the same switch), and at most 6 go out in 10 minutes for switches and the Keychain, and another 6 for the reminders (re-login, expiring login, paused priming), so reminders never crowd out a switch. Each one is cut off after 2 seconds, and a notification that fails never affects the engine's tick.

```bash
cc-swap notify test                      # send a test notification now
cc-swap notify status                    # on or off, per event, and how they are sent (--json)
cc-swap config set notify.enabled false  # no notifications at all
cc-swap config set notify.switch false   # everything but the switches
```

`notify.enabled` (default true) turns them all off; `notify.switch`, `notify.relogin`, `notify.loginExpiring`, `notify.primePaused` and `notify.keychain` turn one kind off. `CC_SWAP_NOTIFY=0` in an engine's environment turns delivery off for that process.

## Fleet: the TUI home for maximize

With `autoswitch.strategy` set to `maximize`, the TUI (`cc-swap` on its own, or `cc-swap tui`) opens on **Fleet** instead of the upstream dashboard; `cc-swap watch` still opens the watch view. The upstream dashboard is in the menu (`m` → `c`, *Classic dashboard*); `ctrl+f` comes back from any screen, and `ctrl+t` toggles the colour theme. Other strategies keep the upstream TUI unchanged.

<img src="../assets/fleet-wide.png" width="760" alt="Fleet at 160 columns: the status sentence and the attention line on top, the capacity summary line, then a table of six accounts under the headers order, account, plan, 5h, 5h resets, 7d, 7d resets and status, with soft/hard-marked bars, both reset times on every row and the status right after them; under it the selected account in full, and the seven-key footer">

- **One sentence on top** says what automatic switching is doing: `Auto ON · using #1 main · 5h 62% past soft 50 — will switch to #2 side when you pause (forced at 98%, ~2h)`. On its right it says who runs the engine: `viewer · service pid 4121 is switching` (`is idle` while automatic switching is off). It never shows an old decision as the current one: with automatic switching off it reads `Auto OFF — nothing switches automatically`, with no engine `Not switching — no engine is running` (or `Not switching — the service is installed but stopped (m to start one)`), right after a switch `waiting for the engine's next check`, and once the engine has published nothing for longer than it should, `the engine has not reported since 14:02 (25m ago) — nothing below is live`. While this TUI runs a dry-run engine, `Dry run` replaces `Auto ON`. During a Fleet re-login it reads `Paused · re-login in progress — nothing switches until 14:10`, and while you hold the active account (`h`, see [cc-swap hold](#staying-on-one-account-cc-swap-hold)) `Holding #1 until 15:30 (2h left) — only hard 98%/100% will move you`. A narrow terminal gets a shorter wording; the line never wraps.
- **The engine's reasons in plain words.** When maximize holds for a reason of its own (the `reset-wait`, `preempt` and `rebalance-deferred` codes of [Why didn't it switch?](#why-didnt-it-switch)), the sentence says what it is waiting for instead of a generic "it stays": `5h 96% — resets in 8m, waiting it out (switches at once if it hits 100%)` (the minutes count down live), `7d 84% would pass 90% in ~3h, before your usual quiet time (23:00) — will move to #2 side when you pause`, `rebalance deferred to your quiet time (23:00)`, past a hard mark with no roomier account (`hard-stay`) `5h 99% >= hard 98% — no account has more room, it stays (switches at once at 100%)`, and a learned ride through the last point (`ride`) `7d 99% — riding to the limit, switching in ~2m (learned) or at your next pause` (the minutes count down live); a `preempt` switch reads `switching #1 main → #2 side now while you're idle`. This works the same whether the service, another process or this TUI runs the engine.

<img src="../assets/fleet-reset-wait.png" width="760" alt="Fleet at 160 columns while maximize waits out a reset: the top line reads Auto ON · using #1 main · 5h 96% — resets in 8m, waiting it out (switches at once if it hits 100%)">

- **At most one attention line**, only when something needs you: a dead login (`! #3 old needs re-login — select it, press r`), a login that ends within a week, priming paused after a Claude Code update, or a Linux service that stops at logout. It is red for a dead login or one in its last day, amber otherwise.
- **A capacity summary** right over the column headers: `5h free: 4 accounts · next 5h back 07:10 (#3) · 7d left this week ≈ 2.3 accounts · next 7d reset Oct 5 12:51`. It counts the accounts automatic switching can use (known 5h and 7d usage; not a dead, expired or excluded login, not an API key). *5h free* are those whose 5h is under its soft mark and whose 7d is under its hard mark (the active one included); *next 5h back* is the soonest 5h reset among the rest whose 7d is under its hard mark; *7d left ≈ N accounts* adds up (100 − 7d%)/100 per account, not weighted by plan (a 20x and a 5x account count alike; `?` says so); *next 7d reset* is the soonest weekly reset. A narrower terminal drops the clocks first, then the 7d part.
- **One table row per account**, under dim column headers: `order · account · plan · 5h · 5h resets · 7d · 7d resets · status`. The account is its short name: its alias when you set one, else the part of its address before the `@` (`dev.shared` for dev.shared@example.com), with the slot number after it as a dim `#4` (the number the attention line and `cc-swap` commands use). Two accounts with the same part before the `@` say where they are from (`jordan.lee@example`, `jordan.lee@uni`), so names stay unique. Set an alias with `cc-swap alias 2 side` or Fleet's `n`; `cc-swap alias 2 --unset` (or an empty name in Fleet) brings the short name back. The top line, the attention line, notifications and the fork's CLI lines (`cc-swap hold`, `auto status`, `why`) use the same names; only the selected account's panel shows the whole address. Each bar marks the soft threshold with an amber `┃` and the hard one with a red `┃`, and its colour follows them: green under soft, amber from soft, red from hard, with that window's own `maximize.soft*`/`maximize.hard*` values. The classic dashboard and the auto screen colour maximize bars the same way. A stale reading is dimmed.
- **When both windows reset, for every account:** `1h47m` under `5h resets` (the countdown alone at every width; the selected account's panel gives the exact time) and `3d19h · Oct  7 02:18` under `7d resets` (a narrower terminal keeps the countdown and drops the clock). A 5h window that is not running reads `not started`, an unknown one `—`; a dead login keeps the resets of its last good reading and says `⚠ needs re-login` where its bars would be.
- **Order:** the `order` column marks the active account `●` and numbers the others `1`, `2`, `3` … in the order automatic switching would try them (the engine's pick order, then the rest); an account it never goes to (a dead login, an excluded account, a login past its deadline) reads `–`. The rows follow that order, excluded accounts last.
- **One status per account**, in its own column right after `7d resets` (never at the terminal's right edge), the most important: `● active`, then `re-login (r)`, `excluded`, `next` (where automatic switching goes next, shown only while it runs), `login 3d left` (`login 5h left` inside the last day, `login expired` past the deadline), `last resort`, and `5h off · prime 05:25` (when priming starts the window; the time is hidden while priming does not run); otherwise a dim `primed` when priming opened the 5h window it is in.
- **The selected account in full** under the table, when the terminal has the rows for it: organization, plan, login deadline (`login ends Oct 24 09:12 (in 21d 0h)`), priming (`5h opened by priming`, `next prime ≤08:30`), and a long bar for every usage window it has, per-model ones such as `Fable` included, each with its exact reset (`resets Oct 7 02:18 (in 3d19h)`).
- **The table is used at every terminal size.** When the columns do not fit, they give way in this order: the bars shorten (down to 6 cells), the columns move closer, the 7d reset clock goes (the countdowns stay), the plan column goes, the name is cut with `…` down to 13 characters (its `#4` stays), and only then do the bars go, leaving the coloured percentages and the name the room. `order`, the resets and the status never go. A short terminal drops the capacity summary first (it shows only where it leaves the selected account's panel its rows, and never under 12 rows), then the panel; the sentence, the attention line, the column headers and the footer stay on screen at every size, and only the table scrolls.

<img src="../assets/fleet-narrow.png" width="560" alt="Fleet at 80x24: the same table with the percentages in place of the bars, countdowns without clocks, nearly whole names with their #N, and the selected account in full below it">

- **Keys.** The footer is the whole list: `enter` switch to the selected account (asks first only when maximize would not land there) · `r` re-login · `l` last resort on/off · `h` hold (stay on the active account: `h`/`t`/`f` one, two or four hours, `u` or just type a time, `o` off) · `m` menu · `?` help · `q` quit. `↑`/`↓` (`j`/`k`) move one row, and a click selects too; the selected row gets a highlighted background and keeps its colours. `n` names the selected account: a small input holds the name the table shows; `enter` saves it as the account's alias (checked like `cc-swap alias`, which accepts letters, digits, `.`, `-` and `_`, refuses a purely numeric name, and refuses a name another account has), an empty name clears the alias so the short name comes back, and `esc` cancels. It is not in the footer, which would no longer fit 80 columns; `?` lists it, and Account settings (`m` → `a` → `n`) does the same. `?` explains every tag and word on the screen (soft/hard, next, last resort, pace, priming, viewer/lease, waiting it out, quiet time, preempt, rebalance deferred, holding, the capacity summary) and says what the engine has learned of your busy and quiet times (`idle pattern: 9 days learned · next quiet window 23:00–07:30`); the home screen itself stays quiet about it.
- **Menu** (`m`), one letter per item: `o` automatic switching on/off · `m` Mode · `s` Swap strategy · `p` Prime now · `f` Fetch latest usage · `x` exclude/include the selected account · `a` Account settings · `e` Engine log (the auto screen) · `v` View switch history · `c` Classic dashboard · `q` Quit; `↑`/`↓` and `enter` work too, `esc` closes it. The letters also work straight from Fleet, except `o` (one stray key never turns switching off) and `m`; `w` opens the watch view and `g` the engine log. **Account settings:** `a` Add current login · `t` Token or API key · `r` Re-login · `n` Name (alias) · `d` Delete account · `i` Inspect all logins (doctor) · `b` back; an item that needs an account, chosen from the list, first asks which one. **Mode:** offers what fits the current engine: with none running, `d`/`l` run an engine here (dry-run/live); with a dry-run engine here, `l` goes live and `s` stops it; with a live one here, `d` goes back to dry-run and `s` stops it; while the service or another process holds the lease, none of these. `o` (automatic switching off/on) is always there. Keys are unique on each screen, and `v` and `i` mean one thing anywhere in Fleet.
- **Swap strategy** (`m` → `s`) edits the four soft/hard marks, `landingMargin`, the idle window and rise, `forceEtaMin`, `resetWaitMin`, the learned ride (`learnedRide`, `rideWindows` — `←`/`→` cycle 7d, 5h, 5h,7d and none — and `rideMaxMin`), the rebalance cooldown and `tieEpsilon`; the idle-pattern knobs `learnIdlePattern`, `preempt`, `preemptHorizonMaxH` and `busyRebalanceGap` (their group's heading says what has been learned so far); and priming (`prime.enabled`, `prime.jitterS`, `prime.maxAttempts`, `prime.model`). `↑`/`↓` (`j`/`k`) pick a value, `←`/`→` adjust it within its range (a soft mark never passes its hard one; on/off values toggle), `e` types one (checked the way `cc-swap config set` checks it), `s` saves to `settings.json` and `b` goes back (with unsaved edits, `b` or `esc` warns first and a second press discards them). A live preview shows what the engine would decide with the edited values next to the saved ones, using the engine's usage history as the engine would. The engine, including the service, picks the change up on its next tick.
- **Viewer by default.** Fleet never takes the engine lease by itself, so it never pushes the service aside. Mode (`m` → `m`) runs an engine in this TUI on request — dry-run, or live after a confirmation — and quitting asks first while a live one runs. The auto screen attaches to that engine instead of starting a second one.
- **Re-login.** A dead refresh token gets a red `re-login (r)` tag and the attention line. `r` on it (or Account settings → Re-login) does what `cc-swap login N` does: Fleet steps aside, `claude auth login --claudeai --email <the account's email>` runs in the terminal in a throwaway profile (`<backup root>/relogin-*`, mode 0700, with every auth and endpoint override stripped from its environment), and you sign in in the browser (over SSH: open the printed URL anywhere and paste the code back). Back in Fleet, cc-swap reads the new login from that profile (macOS: its own Keychain item, `Claude Code-credentials-<hash of the profile path>`; elsewhere its `.credentials.json`; the account from its `.claude.json`), stores it into the slot only if the email, organization and account id match (and the token's owner does not say otherwise), and lifts the slot's dead-login state. The live login is not touched, so nothing switches and the engine keeps running, unless the slot is the active account: then the live login gets the new login in the same locked step as the slot (cc-swap's and Claude Code's locks, the same ones a switch takes), and a failure in either write restores both, so the old login can never be copied back over the new one. A login that could not be stored is kept as an unclaimed entry (`cc-swap unclaimed`). Independently, the slot remembers the re-login (`reloginPin` in `sequence.json`: the new login's and the replaced login's token fingerprints, no tokens): while the slot still holds that new login, cc-swap never backs the replaced login (or a rotation of it, recognised by a login deadline days earlier) up over it, and a switch keeps such a live login as an unclaimed entry instead of dropping it. Routine rotations are not affected. Like `cc-swap add`, `cc-swap login` refuses to run inside a `cc-swap run` session shell. The profile and its Keychain item are deleted afterwards whatever happened (stored, wrong account, Ctrl-C, SIGTERM, error); a profile left by a killed attempt is removed by the next `cc-swap login` or when an engine starts. A wrong account stores nothing and says who signed in and who was expected.

  When claude cannot be launched (no `prime.claudePath` or `~/.local/bin/claude`, or a build without `claude auth login --email`), `r` shows the guided steps instead. The guide first backs up the active account's current login into its slot (and refuses to start, saying why, if that backup cannot be verified), then shows the steps; cc-swap launches nothing itself, so it works the same over SSH: in another terminal run `claude` (the path in `prime.claudePath`, else `~/.local/bin/claude`), type `/login` and sign in as that account's email (over SSH, open the printed URL anywhere and paste the code back), quit `claude`, then press `enter`. cc-swap stores the live login into the slot only if its email, organization and account id match that slot — it refuses a login that belongs to another slot — and switches back to the account that was active. While the guide is open the engine is paused (`pausedUntil` in `autoswitch_state.json`, at most 10 minutes): no switch and no priming. Other machines keep their own logins; repeat the re-login on each machine that needs it rather than copying one login between machines.
- `CC_SWAP_FETCH_ON_OPEN=0` stops Fleet from fetching stale readings once when it opens as a viewer.

### Logins expire

A Claude Code login has a fixed deadline. The token endpoint sets it at `/login`, Claude Code stores it as `refreshTokenExpiresAt`, and refreshing never moves it: in practice it falls 27–30 days after the login. Up to the deadline the access token keeps rotating as normal. The first refresh after it is refused with `invalid_grant`, and only a new `/login` brings the account back. Claude Code warns its own session three days ahead, but a parked slot has no session to warn in, so cc-swap tracks the deadline for every account (the backup copy, or the live login for the active slot):

- **Warnings from 7 days out.** Fleet tags the account `login 3d left` (amber inside the last week, red inside the last day) and names it in the attention line. `r` re-logs an account in early, before anything breaks; a new login starts a new deadline. `cc-swap list` prints a `login expires Oct 9 20:04 (in 6d 2h)` line under the account, red inside the last day; doctor, the engine log and Fleet show deadlines in the same local-time format. The engine log gets one warning per account per day, and inside the last 24 hours the engine sends a [desktop notification](#desktop-notifications-cc-swap-notify) once a day per account (another one once a login needs a re-login).
- **Named when it happens.** A refused refresh after the deadline reads `re-login needed — login expired` (Fleet's Account settings: `re-login needed (login expired)`; engine quarantine reason `login_expired`), not `refresh token dead`. Nothing spent the token, so there is nothing to look for; just log in again. `--json` keeps `usageStatus: relogin_required` and adds `loginExpired: true`.
- **No landing on a dying login.** `maximize` never makes a soft or rebalance switch onto an account whose login expires within `maximize.loginExpiryGuardMin` minutes (default 120). An at-limit or hard fallback can still use it while it works, but never once its deadline has passed. Priming skips accounts past their deadline. A plain `cc-swap switch` rotation skips an account whose login is dead (expired, or quarantined), and `cc-swap switch N` refuses one (exit 1) unless you add `--allow-dead-login` (the current login is still backed up first; the engine skips such an account and tries the next).
- **Re-login.** Every message gives the same fix, `re-login #N: cc-swap login N, or Fleet → select → r` (see above). By hand, run `claude`, `/login` as that account, then `cc-swap add`. The Fleet guide stores the login only once the live refresh token has changed, so pressing `enter` before logging in stores nothing.
- **Refresh audit.** Every refresh POST cc-swap makes logs one INFO line to the engine log (`refresh POST caller=… slot=… active=… source=live|backup|profile rt=<8 hex>-><8 hex> accessExp=… login=… result=… latency=…`). It holds fingerprint prefixes only, never tokens or emails, so it is safe to paste into an issue when you need to know which machine spent a token.

## Classic dashboard and watch view

With a strategy other than `maximize`, `cc-swap` on its own (or `cc-swap tui`) opens upstream's full-screen dashboard: live usage for every account, switching, and the auto-switcher, all keyboard-driven. Under `maximize` it is in Fleet's menu (`m` → `c`). `cc-swap watch` opens it straight on the live monitor. It works on macOS, Linux and Windows. From the classic dashboard, `g` opens the auto screen.

<img src="../assets/tui-watch.png" width="760" alt="The watch view: live 5h/7d usage bars for every account, with reset times and the active account marked">

## Diagnostics: `doctor`, `init`, `why`

`cc-swap doctor` checks this machine and every stored login in one read-only pass and prints one fix line per problem. It never refreshes a token, writes nothing, and runs no `claude` other than `claude --version`. Accounts appear by slot number and logins by an 8-hex fingerprint prefix, never a token or an email, so the output is safe to paste into an issue. `--json` prints the same findings for scripts. The exit status is 0 when everything is fine, 1 with warnings and 2 with errors (info findings do not count).

| Check | What it looks at |
|---|---|
| `claude` | `prime.claudePath`, then PATH, then `~/.local/bin/claude`; whether `claude --version` answers |
| `keychain` | macOS: whether the live login's Keychain item reads cleanly. `rc=36` (errSecInteractionNotAllowed): the login keychain is locked, the session cannot reach it (SSH, launchd before login), or a `/login` is still writing. `rc=51` (errSecAuthFailed): the item's access control refuses `/usr/bin/security`. `rc=44`: no item |
| `plaintext` | `~/.claude/.credentials.json`: on macOS, a duplicate or a stale copy of the Keychain login (fingerprint prefixes compared); on Linux, readable by other users. A file with only MCP logins (`mcpOAuth`, no `claudeAiOauth` — what Claude Code writes there while the Keychain is unavailable) is no login: on macOS an info line, never "stale"; on Linux "no live login". cc-swap never reads it as the login (a switch does not back it up into a slot, `cc-swap add` and export refuse it), never writes a login into it (a Keychain switch only bumps its mtime), and still carries its MCP logins into the activated login |
| `live-login` | the live login (`~/.claude.json` and its token) belongs to a slot, and its token is not another slot's |
| `upstream` | upstream `claude-swap` still installed (uv or pipx), running, or its menu bar LaunchAgent installed |
| `service` | installed and running; its file written by cc-swap 0.2.0 or later (`CC_SWAP_SERVICE`); pinned to this `cc-swap` and version; its process started after the last install; the same `CLAUDE_CONFIG_DIR` as this shell |
| `lease` | who holds the engine lease (the service, another engine, nobody) and whether a re-login paused switching |
| `hold` | info only: an account hold (`cc-swap hold`) on the active account and until when, or one left over on a slot that is no longer active |
| `settings` | `settings.json` parses and every value is in range |
| `priming` | while priming is on: paused after a Claude Code update until the engine re-verifies it (`prime.autoVerify`) or `cc-swap prime verify` passes (a warning), or the version its isolation was verified for |
| `learned-ride` | info only, strategy `maximize`: the share of the last point each window rides (*Riding the last point*), with how many rides switched before 100% and how many hit it |
| `idle-pattern` | info only, strategy `maximize`: what has been learned of your busy and quiet times |
| per slot | stored login present and readable, login deadline (expired, or under 7 days), quarantine, two slots holding the same login |

In Fleet, `m` → Account settings → `i` (*Inspect all logins*) runs the same checks in a modal; `r` runs them again.

`cc-swap init` is the onboarding and migration checklist. It prints `ok`, `FIX` or `TODO` for each step: Claude Code installed → logged in → the live login saved in a slot → two or more accounts → upstream claude-swap gone → strategy `maximize` → service running on this build (on a platform without a service, this step is ok and says to run `cc-swap auto` in a terminal) → priming off unless verified. It exits 1 until every step is ok, so re-run it after each one. After the steps it also runs the doctor checks and lists any problems they find (these do not change its exit status; `--json` adds them as a `doctor` block). Without `--apply` it writes nothing; `cc-swap init --apply` does the two idempotent steps (`cc-swap config set autoswitch.strategy maximize`, and `cc-swap service install` once the login, slot and upstream steps are ok).

### Why didn't it switch?

`cc-swap why` prints the decision the running engine last published to its state file, if it is fresh (the rule Fleet's top line uses) and about the account that is live now, and explains its reason code with the table below. Otherwise it runs `cc-swap auto --once --dry-run` to show what a tick would decide now (`--no-fallback` skips that, `--json` is for scripts). The same codes appear as `no switch: <code>` in `cc-swap auto` output and the engine log.

| Code | Meaning | What to do |
|---|---|---|
| `below-threshold` | The active account is below autoswitch.threshold (strategies best and consume-first). | Nothing; lower autoswitch.threshold to switch earlier. |
| `cooldown` | A proactive switch happened less than autoswitch.cooldownSeconds ago. | Wait, or lower autoswitch.cooldownSeconds. |
| `no-candidates` | No other account can take you: every other one is disabled, excluded, quarantined or an API key. | cc-swap add another account, cc-swap enable one, or re-login a quarantined one (cc-swap doctor). |
| `no-qualifying-candidate` | Other accounts exist, but none is far enough below the thresholds, or their usage is unreadable this tick. | Wait for a reset (cc-swap list shows when), or loosen the thresholds. |
| `no-comparison` | No candidate's usage could be read this tick. | Check the network and cc-swap list; if it persists, cc-swap doctor. |
| `no-viable-target` | Every candidate failed the last-moment check (dead token, another account's login, live session). | cc-swap doctor names the broken slots; re-login them. |
| `reset-unknown` | consume-first: the active account's weekly reset time is unknown, so it cannot compare. | Nothing; it resumes once usage reports the reset. |
| `already-consuming-soonest` | consume-first: no account with room resets sooner than the active one. | Nothing; this is the strategy working. |
| `stale-usage` | consume-first: the target's usage could not be refreshed this tick (backoff or another poller). | Nothing; it retries next tick. |
| `active-usage-unknown` | The active account's usage could not be read; failover follows after autoswitch.unhealthyTicks misses in a row. | Check the network and cc-swap list; if it persists, cc-swap doctor. |
| `active-idle` | The active access token expired while Claude Code is idle; it refreshes on next use. | Nothing. |
| `active-api-key` | The live login is a managed API key, which has no quota to watch. | cc-swap switch to a subscription account. |
| `active-credential-unreadable` | The live login could not be read cleanly (Keychain rc=36/51, or only a stale plaintext copy), so switching would overwrite a login cc-swap cannot see. | cc-swap doctor; unlock the login keychain. Switching resumes by itself once a read succeeds. |
| `unmanaged-active-account` | The live login belongs to no slot, or to a different account than the slot it claims. | cc-swap add (after checking which account you are logged in as). |
| `new-login-not-backed-up` | A new /login on the active slot is not backed up yet; the engine waits until it is. | Wait a tick; if it persists, cc-swap add --slot N. |
| `no-active-account` | Nobody is logged in to Claude Code. | Run claude and /login, then cc-swap add. |
| `already-active` | The chosen target was already the live login when the switch ran. | Nothing. |
| `maximize-paused` | A Fleet re-login paused switching (pausedUntil, at most 10 minutes). | Finish or cancel the re-login; the pause also ends by itself. |
| `auto-off` | Automatic switching is off (cc-swap auto off, or Fleet: m → o): the engine keeps deciding but never switches or primes. | cc-swap auto on (or Fleet: m → o); cc-swap auto status shows who turned it off and when. |
| `maximize-pending` | A soft mark is crossed; maximize waits for an idle moment (idleWindowMin) before switching. | Nothing; a hard ceiling switches at once. Lower maximize.idleWindowMin to switch sooner. |
| `maximize-hold` | maximize sees no reason to move: below every soft mark and no better-scored account (or within rebalanceCooldownMin, or the better-scored one's 7d would pass soft7d before your next quiet time, so preempt would only move you off it again). | Nothing. |
| `reset-wait` | A hard or soft mark is crossed, but that window resets within maximize.resetWaitMin minutes and the recent pace will not reach 100% before then, so maximize waits for the reset instead of switching (a switch makes Claude Code re-read the whole context on the new account). | Nothing; it switches at once if the window hits 100%. Set maximize.resetWaitMin to 0 to switch without waiting. |
| `preempt` | The active account's 7d is on pace to pass soft7d before your next quiet time, and another account would not; maximize moves at the next idle moment (after rebalanceCooldownMin). | Nothing; set maximize.preempt to false to wait for the soft mark instead. |
| `rebalance-deferred` | A better-scored account exists, but this is usually a busy time and the gain is under maximize.busyRebalanceGap, so the move waits for your next quiet window (at most 6 hours away). | Nothing; lower maximize.busyRebalanceGap, or set maximize.learnIdlePattern to false, to rebalance at any idle moment. |
| `hard-stay` | A hard mark is reached (or close at the recent pace), but no account under the hard caps has more room on that window than the active one, so maximize stays on it rather than move somewhere that would force a move straight back. | Nothing; at 100% the at-limit switch moves you at once to whatever has quota left. cc-swap add another account for more room. |
| `ride` | A window listed in maximize.rideWindows reached a hard mark of 99% or more but is under 100%. Usage is reported in whole percents, so up to one point is left: maximize keeps using it for a learned share of the time one point takes, then switches (at once if the account goes idle first, or if it hits 100%). | Nothing; cc-swap doctor shows what has been learned. Set maximize.learnedRide to false (or maximize.rideWindows to "") to switch at the hard mark, or lower maximize.rideMaxMin to cap the ride. |
| `hold` | You asked to stay on the active account (cc-swap hold, or Fleet: h) so a long task keeps its context: until the hold ends, maximize skips its soft, preempt and rebalance moves. A hard mark, 100% and a reset-wait still switch. | Nothing; cc-swap hold off (or Fleet: h → o) lifts it. It ends by itself at its end time (at most 24h), or when the active account changes. |

## Managing accounts

### Add accounts

Log in to Claude Code with an account, then run `cc-swap add`. Repeat for each account. Do not run `/logout` first: current Claude Code may revoke the refresh token stored for the account you are leaving.

```bash
cc-swap add                 # save the live login (updates the slot if the account is already stored)
cc-swap add --slot 3        # into a specific slot (prompts before overwriting)
cc-swap add --alias dev     # and give it a short alias
```

`cc-swap add` on an account cc-swap already has updates its stored credentials without creating a duplicate; that is how you refresh an expired or revoked login (log in with that account again, then `cc-swap add`, or `cc-swap add --slot N` for a quarantined slot).

### Aliases, slots and removal

```bash
cc-swap alias 2 dev                    # alias usable anywhere NUM|EMAIL is (switch, remove, run, map)
cc-swap alias alice@example.com dev
cc-swap alias 2 --unset
cc-swap alias                          # list all aliases
cc-swap swap 1 2                       # exchange two accounts' slot numbers
cc-swap move 2 1                       # assign account 2 to slot 1 (relocates, or swaps if taken)
cc-swap remove 2
cc-swap disable 2                      # hold out of auto-rotation (keeps its login)
cc-swap enable 2
cc-swap unclaimed                      # list stashed credential entries (slot + why they were stashed)
cc-swap unclaimed --purge ID           # drop one (deletes its bytes; recover with /login + `cc-swap add`)
```

An alias may contain letters, digits, `.`, `-` and `_`, and must not be purely numeric or already used by another account. With `swap` and `move`, aliases, backups and session history move with their account.

### Switching by hand

```bash
cc-swap switch                       # rotate to the next account
cc-swap switch 2
cc-swap switch alice@example.com
cc-swap switch dev                   # by alias
cc-swap switch --strategy best       # most quota left
cc-swap switch --strategy next-available   # skip rate-limited accounts
```

`cc-swap list` is the dashboard: every account's 5-hour and 7-day usage and reset times at a glance.

**Do you need to restart after switching?** Usually not. On **Linux and Windows**, credentials are stored in a file and Claude Code re-reads them whenever that file changes, so the new account takes effect on your next message. On **macOS**, credentials live in the Keychain, which Claude Code caches for about 30 seconds; a running session picks up the switch once that cache expires. Restart Claude Code (or close and reopen the VS Code extension tab) only if you want the change to apply instantly.

**Continuing sessions after switching.** You can keep using the same Claude Code session after switching: run `cc-swap switch` in any terminal and carry on. If you'd prefer a clean start, close and reopen Claude Code (or the VS Code extension tab) and use `--resume` to pick your previous session. Either way, the first message on the new account may use extra usage as its conversation cache rebuilds.

### Add an account from a raw token or API key

If you only have a long-lived setup-token (for example from `claude setup-token`) or a managed API key (`sk-ant-api...`) and don't want to log in through the browser first (useful on headless servers, or when receiving a token from another machine), register it directly. The token type is detected automatically:

```bash
cc-swap add-token sk-ant-oat01-...             # OAuth setup-token
cc-swap add-token sk-ant-api03-...             # managed API key
cc-swap add-token sk-ant-oat01-... --slot 3
cc-swap add-token - --slot 3                   # read the token from stdin
cc-swap add-token --email alice@example.com    # optional label override
```

`--email` is optional; without it the label is `setup-token-{slot}@token.local` (or `api-key-{slot}@token.local` for API keys). No Anthropic API calls are made.

**API-key accounts.** An `sk-ant-api...` value registers a managed API-key account (the kind Claude Code uses after `/login` with a key) rather than an OAuth setup-token. It switches like any other account; since API keys have no subscription quota, they show no usage and the usage-aware `switch` strategies never skip them as rate-limited. Automatic switching never rotates onto them unless `autoswitch.includeApiKeyAccounts` (or `--include-api-key-accounts`) allows it.

## Session mode: run several accounts at once

Launch Claude Code as a specific account in the current terminal only. Every other terminal and the VS Code extension stay on your default account, so two accounts can work in parallel. (The command is marked experimental.)

```bash
cc-swap run 2                     # launch Claude Code as account 2, here only
cc-swap run alice@example.com     # by email
cc-swap run 2 -- --resume         # everything after '--' is forwarded to claude
cc-swap run 2 --share-history     # share your chat history with this account too
cc-swap run 2 --require-session   # refuse rather than run plain claude if 2 is the default login
cc-swap run 2 --no-share          # don't share your ~/.claude setup into the session
```

Sessions use your normal `~/.claude` setup (settings, keybindings, CLAUDE.md, skills, commands, agents, MCP servers), but each account keeps its own chat history. Pass `--share-history` if you want your accounts to continue the same conversations (history the profile already accumulated is merged into `~/.claude` first; `--no-share-history` restores per-account history; not supported on Windows). `run` must be the first argument (`cc-swap run 2 --debug`, not `cc-swap --debug run 2`).

Running the account that is already your default login launches plain `claude` on that login instead of a session (a second copy of the active credential would go stale). Scripts that need the isolation guaranteed can pass `--require-session`, which refuses in that case instead.

A session refreshes its own copy of the account's token, so once it exits, the credential it rotated is captured back into the account's stored backup before a switch or usage check uses that backup. While a session is still running, `cc-swap switch` refuses to move the default login onto its account if the stored backup has already fallen behind (activating it could only fail); exit the session first, or pick another account. While a session runs, its account's usage is read with the session's own credential and never refreshed by cc-swap; a read the server refuses shows as token expired, and is not requested again, until the session renews the credential on its next call. `cc-swap purge` refuses while a session-mode `claude` is running.

Sharing details:

- With `--share-history`, a session started under one account shows up in `--resume` under the others, and nothing already saved is lost.
- User-scope MCP servers (`claude mcp add -s user`) are mirrored from your default profile on every launch; manage them there, because changes made inside a session don't persist. Definitions are copied as-is (including inline `env`/`headers` values), but MCP OAuth logins are not: HTTP servers may ask you to authenticate once per profile via `/mcp`.
- `--no-share` turns sharing off: it stops sharing settings, keybindings, CLAUDE.md, skills, commands and agents, removes previously shared items, and removes the mirrored MCP config (profiles that never mirrored are left alone).

### Map accounts to directories

Bind a directory to an account, and a bare `cc-swap run` there launches that account in session mode, for example a work account in work repos and a personal one elsewhere:

```bash
cc-swap map 2 ~/work/client-app   # map a directory to account 2
cc-swap map alice@example.com     # map the current directory
cc-swap map                       # list mappings
cc-swap unmap ~/work/client-app   # remove one (defaults to the current directory)

cd ~/work/client-app/src
cc-swap run                       # → account 2, session mode
```

Subfolders inherit the nearest mapped ancestor. In an unmapped directory, `cc-swap run` just launches plain `claude` with your default login. Mappings are per-machine (not part of `cc-swap export`) and are cleaned up when their account is removed.

## Backup and migration

Move account data between machines or back it up:

```bash
cc-swap export backup.cswap                    # all accounts to a file
cc-swap export backup.cswap --account 2        # one account
cc-swap export backup.cswap --full             # include the full ~/.claude.json and credential object (same-PC backup)
cc-swap import backup.cswap                    # skips accounts that already exist
cc-swap import backup.cswap --force            # overwrite existing ones
```

The export file is plaintext JSON and, by default, carries only each account's own login: machine-shared MCP/plugin OAuth tokens and the device token stay on the source machine (`--full` keeps everything, for same-PC backups). If you need encryption, pipe it through your tool of choice (for example `cc-swap export - | gpg -c > backup.gpg`).

A plain import replaces slots whose refresh token is dead (see [the `best` and `consume-first` strategies](#the-best-and-consume-first-strategies)); `--force` is needed to replace any other existing account. If an imported account is the one you're currently logged in as, activate the imported credentials with `cc-swap switch N --force` (a plain `switch` to the current account is a safe no-op and won't touch the import).

For several machines, prefer logging in on each machine separately: each machine then has its own token chain, and a login moved by export/import shares one with the source machine.

## Share usage readings between machines

Machines that hold the same accounts can end up spending one usage-endpoint budget: when they share a login (moved between them with `export`/`import`, so the same token is live on each), or when the account's usage requests are limited per account rather than per token. Their polling then adds up. `import-usage` lets one machine poll and hand its readings to the others:

```bash
cc-swap list --json | ssh laptop cc-swap import-usage - --hold 600
```

The input is `cc-swap list --json` output (`-` reads stdin). Each row with `usageStatus: "ok"` is matched to a local account by email and organization, and adopted when it is newer than the reading already stored. Its age comes from `usageAgeSeconds`, so the two machines' clocks never have to agree; a script that delays the hand-over should add the delay to that field. `--hold SECONDS` keeps every collector on the receiving machine (`list`, `status`, `auto`, the dashboard, the menu bar) from fetching those accounts for that long, and the held reading stays trusted for switch decisions meanwhile. A hold never runs past the reading's earliest window reset (per-model windows included), nor past an hour after the reading was taken. Renew it with each hand-over, or lift it early with `--hold 0`; when it lapses, the machine goes back to fetching for itself.

## JSON output for scripting

Add `--json` to `list`, `status`, or `switch` to emit a single machine-readable JSON object on stdout (human-readable notices go to stderr). Useful for scripting auto-swap and quota tracking.

```bash
cc-swap list --json                   # all accounts with usage/quota
cc-swap status --json                 # current active account
cc-swap switch --strategy best --json # switch, then report the result
cc-swap switch 2 --json
```

Example:

```json
{
  "schemaVersion": 1,
  "activeAccountNumber": 2,
  "accounts": [
    { "number": 2, "email": "alice@example.com", "active": true, "usageStatus": "ok",
      "usage": { "fiveHour": { "pct": 25.0, "resetsAt": "2026-06-22T23:29:59Z" },
                 "sevenDay": { "pct": 16.0, "resetsAt": "2026-06-26T17:59:59Z" } } }
  ]
}
```

Every payload carries a `schemaVersion` (currently `1`); on a handled error stdout is `{"schemaVersion":1,"error":{...}}` with a non-zero exit code. `--switch`/`--switch-to` report `{"switched": true|false, "from": …, "to": …, "reason": …}`.

Usage is served from a per-account cache: when the usage API is briefly unreachable, the last-known numbers are shown instead of nothing (the human view marks them with their age, for example `· 2m ago`). Rows with decision-trusted usage carry additive `usageFetchedAt`/`usageAgeSeconds` fields telling you how old the measurement is. Whenever `usage` is null but a last-known measurement exists (data too old to drive a decision, where `usageStatus` stays `unavailable`, or a row in a non-`ok` state such as `token_expired`), additive `lastGoodUsage`/`lastGoodFetchedAt`/`lastGoodAgeSeconds` fields preserve the human display without making the account actionable. When `usage` is null and nothing else explains it (`usageStatus` is `unavailable`), an additive `usageError` names the last fetch failure by kind (for example `http-429`, `timeout`) and, while the cache is backing off from it, `usageRetryAt` gives the time of the next attempt. These fields apply to list rows and the managed active row from `status --json`. An account held out of rotation with `cc-swap disable` carries an additive `"disabled": true` on its row (absent otherwise).

A row carries an additive `loginExpiresAt` (ISO-8601 UTC) when the stored login records when its refresh token expires, which is the moment the slot will need a fresh `/login` and `cc-swap add --slot N`; a script can warn a few days ahead instead of discovering `relogin_required`. It is absent when Claude Code recorded no such date for that login. Once that moment has passed the row also carries `"loginExpired": true` (derived from the stored date, so it can flip a few hours before the server refuses the next refresh).

An account row also carries an additive `alias` field once one is set with `cc-swap alias` (for example `"alias": "dev"`); accounts without one omit the key.

Weekly windows (`sevenDay` and per-model `scoped` entries, never `fiveHour`) additively carry pace fields once the week is about a day old: `expectedPct` (where usage would sit if spread evenly across the week) and `aheadOfPace` (`true` when meaningfully above that, the same signal the human views show as an `(ahead)`/`(ahead of pace)` marker). `projectedExhaustionAt`/`willLastToReset` extrapolate the current rate into an ETA to 100% and a yes/no "will it last to the reset"; they stay `--json`-only since a linear projection is too rough to present as fact in the UI.

`cc-swap auto --json` emits an event *stream* instead: one JSON object per line (`{"schemaVersion":1,"event":"switch","ts":…, …}`) with kinds `poll`, `switch`, `no-switch`, `account-quarantined`, `account-unquarantined`, `login-adopted`, `all-exhausted`, `sleep`, `error`, `config-warning`, `maximize` and `prime` (the base kind is `event`). The contract is additive: new kinds and fields may appear, so scripts should ignore unknown ones.

Other commands with `--json`: `cc-swap doctor`, `cc-swap init`, `cc-swap why`, `cc-swap history`, `cc-swap hold`, `cc-swap auto status`, `cc-swap notify status`, `cc-swap prime verify` and `cc-swap config list|get`.

## Installing, upgrading and migrating

### Install and upgrade

```bash
uv tool install git+https://github.com/wonjun-lab/cc-swap
uv tool install git+https://github.com/wonjun-lab/cc-swap@cc-v0.5.1   # pin a release tag
uv tool install 'cc-swap[menubar] @ git+https://github.com/wonjun-lab/cc-swap'   # with the macOS menu bar extra
```

Requirements: Python 3.12+, and Claude Code installed and logged in.

From source:

```bash
git clone https://github.com/wonjun-lab/cc-swap.git
cd cc-swap
uv sync
uv run cc-swap help
```

To upgrade, run `cc-swap upgrade` (alias `cc-swap update`). It installs the latest fork release tag. If the service is installed, `cc-swap upgrade` re-runs `cc-swap service install` for you after a successful reinstall, so the service restarts on the new build. (Reinstalling by hand with `uv tool install --force git+https://github.com/wonjun-lab/cc-swap` leaves the service on the old build until you run `cc-swap service install`.) `cc-swap upgrade --force` reinstalls even when you are already on the latest release. The menu bar's own LaunchAgent is not refreshed; see [Menu bar](#menu-bar-macos).

`cc-swap upgrade --check` only reports: it prints the installed and the latest release, then the release notes and commit subjects in between. It exits `0` when you are up to date, `10` when an update is available, `2` when GitHub could not be asked and the cache cannot settle it (see below) and `1` when no release is published yet, so a timer can run it daily. `--check` is accepted only with `upgrade`.

`upgrade` and `upgrade --check` look the latest release up on the GitHub API, which allows only 60 anonymous requests an hour per IP. They authenticate when they can: with `$GITHUB_TOKEN` or `$GH_TOKEN` if set, otherwise with the token from `gh auth token` when the GitHub CLI is on your `PATH` (given 3 seconds to answer). The token is sent only to `api.github.com`, as `Authorization: Bearer`, and is never printed or logged; with no token, or one GitHub rejects, the lookup goes out anonymously.

When the lookup fails (rate limited, offline, ...) they say so on stderr, for example `cc-swap: could not reach GitHub (rate limited until 05:12); using cached cc-v0.3.1 from 2h ago — it may be out of date`. A cached release never counts as proof that you are current: if it matches or trails what you run, `upgrade` prints `cannot confirm the latest release`, changes nothing and exits `2` (`upgrade --check` exits `2` too, without saying "up to date"; `upgrade --force` still reinstalls the cached release). A cached release newer than what you run is still installed (`upgrade`) or reported as an update (`upgrade --check`, exit `10`). With nothing cached, `upgrade` installs the default branch and says so. The passive update notice (checked at most once a day before other commands) stays fast: one request with a 2-second timeout, authenticated only by `$GITHUB_TOKEN` or `$GH_TOKEN` — it never runs `gh` and never retries without a refused token — and it stays silent when it cannot reach GitHub.

`cc-swap upgrade`, `upgrade --check` and the update notice read the fork's releases list and consider only `cc-v` tags (see [Releasing](releasing.md) for why).

### Migrating from cswap

cc-swap reads and writes the same data as claude-swap: the backup directory (`~/.claude-swap-backup`, or `~/.local/share/claude-swap` on Linux) and the `claude-swap` Keychain items. Your accounts carry over without logging in again. The two tools must not run side by side, because two engines would fight over the active account.

1. Close every running `cswap`: TUI windows, `cswap auto` loops, cron jobs that run `cswap auto --once`, and the menu bar's *Auto-switch accounts* toggle.
2. `uv tool uninstall claude-swap`
3. `uv tool install git+https://github.com/wonjun-lab/cc-swap`
4. `cc-swap config set autoswitch.strategy maximize`. The service runs plain `cc-swap auto`, which reads its strategy from the settings.
5. `cc-swap service install`

`cc-swap init` checks these steps for you (and `cc-swap init --apply` does steps 4 and 5), and `cc-swap doctor` explains anything left (see [Diagnostics](#diagnostics-doctor-init-why)).

To go back, run `cc-swap service uninstall`, `uv tool uninstall cc-swap` and `uv tool install claude-swap`. Upstream warns about the unknown `maximize` strategy, falls back to `best`, and ignores the `maximize`/`prime` settings.

### Uninstall

```bash
cc-swap service uninstall   # if you installed the service
cc-swap menubar --uninstall-service   # if you installed the menu bar agent
cc-swap purge               # remove all claude-swap data
uv tool uninstall cc-swap
```

## Menu bar (macOS)

The fork still ships upstream's optional macOS menu bar app. It needs the `menubar` extra:

```bash
uv tool install 'cc-swap[menubar] @ git+https://github.com/wonjun-lab/cc-swap'
cc-swap menubar
```

It shows every account's 5h / 7d / spend usage and switches with a click (specific / rotate / best / next-available), plus the TUI's add / disable-enable / remove / refresh actions. Enable *Settings → Auto-switch accounts* to run the same engine as `cc-swap auto` in the background; it shares the `autoswitch.*` settings, so the menu bar and CLI stay in sync. It is off until you turn it on. While the cc-swap service (or another engine) holds the engine lease, the toggle reads *(running in another cc-swap engine)* and the menu bar only displays. With the service installed you don't need the menu bar's auto-switch.

**Keep it running without a terminal.** `cc-swap menubar` runs in the foreground, so the status item dies with the terminal that started it and does not come back after a reboot. `--install-service` hands it to launchd instead (starts at login, restarts on crash, no `.app` bundle):

```bash
cc-swap menubar --install-service     # start now, and at every login
cc-swap menubar --service-status      # installed? loaded? pid?
cc-swap menubar --uninstall-service   # stop it and remove the plist
```

The agent lives at `~/Library/LaunchAgents/com.cswap.menubar.plist` and logs to `~/Library/Logs/com.cswap.menubar.{log,err}`. It pins the `cswap` console script, whose path survives an upgrade, but the running process keeps the old build until it restarts, so after `cc-swap upgrade` either re-run `--install-service` or `launchctl kickstart -k gui/$(id -u)/com.cswap.menubar`. This agent is separate from the cc-swap service.

## How it works

- Backs up OAuth tokens and config when you add an account.
- Swaps only the account-specific Claude login when you switch accounts; live account-independent OAuth state (such as MCP server logins) is preserved instead of being overwritten by a slot's older snapshot.
- Stores account credentials using platform-appropriate methods (see [Data locations](#data-locations)).
- Switches (manual and automatic) hold Claude Code's own credential locks while writing, so a swap never interleaves with a token refresh.
- Auto-switch freshens a target's token before activating it, and quarantines accounts whose refresh token has died (recover by re-adding it with `cc-swap add --slot N`, or by replacing its stored credentials from a known-good export: a plain `cc-swap import backup.cswap` replaces dead-token slots automatically).
- Usage numbers refresh every few minutes: faster for an account being used or close to switching, slower for idle ones, keeping cc-swap comfortably inside Anthropic's rate limits however many dashboards you keep open on a machine. An age note like `· 6m ago` just means the next scheduled check hasn't come yet, not that something is stuck.
- Works with both the Claude Code CLI and the VS Code extension.

## Data locations

| Platform | Credentials | Config backups (the "backup root") |
|----------|-------------|----------------|
| Windows | File-based (inside the backup directory, under `credentials/`) | `~/.claude-swap-backup/` |
| macOS | macOS Keychain (`claude-swap` items) | `~/.claude-swap-backup/` |
| Linux / WSL | File-based (inside the backup directory, under `credentials/`) | `${XDG_DATA_HOME:-~/.local/share}/claude-swap/` |

On Linux/WSL, set `XDG_DATA_HOME` to override the default location.

Files in the backup root:

| File | What it holds |
|---|---|
| `settings.json` | Settings (`cc-swap config path` prints its location) |
| `autoswitch_state.json` | Auto-switch state: cooldown and quarantined accounts, `autoOff`, `accountHold`, `pausedUntil`. Delete it to reset |
| `auto_off.json` | Authoritative copy of `cc-swap auto off` |
| `hold.json` | The account hold |
| `switches.jsonl` | Switch history (0600, rotates at 1 MiB, three generations) |
| `usage_history.jsonl` | Usage history the idle-pattern learning reads |
| `notify_state.json` | Notification deduplication and rate limits |
| `prime_verify.json` | The Claude Code version priming was verified with |
| `claude-exec.jsonl` | Every `claude` cc-swap ran (0600, moves to `.1` past 2 MB) |
| `claude_exec_state.json` | The `claude` files seen and run, a killed-by-the-OS mark, kills of `claude` cc-swap did not launch and the engine's probe of them, a settle wait |
| `claude-kill-<time>.txt` | Diagnostics of a `claude` the OS killed |
| `codesign-events.jsonl` | Code-signature messages from `log stream` and code-signing crash reports of `claude` (macOS; moves to `.1` past 2 MB) |
| `prime-profile/` | Isolated config directory for priming runs |
| `sessions/` | Session-mode profiles (`cc-swap run`) |
| `.engine.lock` | The engine lease |

Service files and logs: macOS `~/Library/LaunchAgents/com.wonjun-lab.cc-swap.plist` and `~/Library/Logs/cc-swap/`; Linux `~/.config/systemd/user/cc-swap.service` and the journal.
