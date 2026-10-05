# Review: v0.5.10 candidate branches

Base: `origin/main` @ `3fdc9b9` (cc-v0.5.9). Review only: no code was changed on any branch.

| Branch | Commits | Verdict |
|---|---|---|
| `feat/ride-to-99-9` | `e63536d`, `710f4b8` | **Not mergeable yet.** Two should-fix items (R1, R2) are required first. R3 and R4 are strongly recommended. |
| `fix/doctor-service-stale-race` | `2c9c54d` | **Mergeable: yes** |
| `chore/stale-urgent-comments` | `ee87e0b` | **Mergeable: yes** |

**Tests.** I ran the full suite on each branch and on main. Every failure is environmental: the CLI subprocess tests refuse to run as root ("Do not run this script as root"). The set of failing maximize tests is identical on `feat/ride-to-99-9` and on main (28 in `tests/maximize`, 38 overall). Everything else passes, including all new ride tests and `test_ride_controller.py`. `tests/maximize/test_doctor.py` passes on the doctor branch (78 passed). On the chore branch, `test_engine_maximize.py` and `test_switcher.py` show the same 2 root-only failures as main.

Severity scale: **blocker** means do not ship. **Should-fix** means it is wrong or can measurably raise the hit rate, so fix it before the release. **Nit** means polish, docs or follow-up.

---

## 1. `feat/ride-to-99-9`

### Summary of the change

- **q controller.** The halving AIMD becomes a target-hit-rate controller (+0.01 for a clean ride, −0.09 for a hit, so p* = 10%). It has a +0.05 "slow start" until the first hit. q now lies in [0.3, 0.95] and starts at 0.6.
- **T1.** T1 is now the pooled pace of the last 6 timed steps (slow outliers over 2× the median are dropped). The velocity overrides it only on a burst of 2 or more points. The old rule took `min(steps median, velocity)`.
- **Sub-point measurement on the 7d.** The 7d's last point is measured on the 5h as `used = k × (rise5 + phase_now − phase_armed)`. It switches at a learned `t` in [0.5, 0.97] (start 0.85, same 10% controller). k comes from `drain.learn_k`, and only a *learned* k is accepted (`policy.ride_k`).
- **Refusals count as hits.** A limit refusal reported by Claude Code (`Estimate.refused`) now counts as a hit.
- **`rideMaxMin` default goes from 30 to 60.**

The overall design is sound. The 5h measure is a real improvement over timing, and the controller arithmetic is right: with steps u and d, the long-run hit rate is u/(u+d) away from the clamps.

I checked the following and found them **correct**:

- **No ride on an unlisted window.** `rideable` (`policy.py:552`) still gates every ridden window on `ride_windows(s)`. `ride_used_5h` is 7d-only (`policy.py:632`). Hits are attributed only to windows in `record["riding"]`. A refusal for the *other* window (for example, 5h refused during a 7d ride) sets that window to 100% only. It is not a 7d hit, and the 5h's own hard/at-limit path switches.
- **No ride while backing off or projected.** `_hard_or_ride` still returns the hard switch on `active_recent_429` and on any `snap.estimate` (projected or reported) before it plans the ride (`policy.py:734-737`). Arming and folding continue in the background, which is fine (but see R1 for what that later implies).
- **At-limit and refusals.** A reported refusal makes `estimate` non-None, so the same tick switches `at-limit`. `_ride_track` sees `refused` before the decision and records the hit once: the armed entry is popped, and `riding` is filtered on the next commit.
- **Idle.** The idle switch still precedes the Hold, carries `ride_by_5h`, and teaches nothing.
- **Hold.** Unchanged: the ride is the hard switch, and an account hold does not block it.
- **Drain.** It shares `drain.learn_k`. The only drain-side change is that history points are now also recorded when only the 7d ride needs k (`engine_hook.py:786`, `view.py:356`). It is consistent between engine and view.
- **Multi-machine.** The usage endpoint is account-wide, so the 5h and 7d both include every machine's use, and k = Δ7d/Δ5h is the account's ratio, not this machine's. Another machine's use does not "inflate" the 5h relative to the 7d. Both rise together and the measure stays consistent. Idle detection is also account-wide (another machine's use makes the account non-idle), as before. The remaining multi-machine effect is that each machine learns its own q and t from the same account's rides. That is harmless.
- **The 5h cap during a 7d ride.** When the 5h reaches its hard cap, `force.windows` includes 5h, `rideable(5h)` is false, and it switches at once (no ride on the unlisted 5h). That switch is not `ride="due"`, so it teaches nothing.

### Findings

#### R1 — should-fix: a large gap before the arming reading biases the 5h estimate low and rides into 100%

`engine_hook.py:1000` (`_arm_five_h`: `phase += (read_at - at) / 2.0 / point`), with `ride.arm_time` (`ride.py:151`) accepting a previous reading up to `STEP_MAX_GAP_S` = 30 min old.

The 5h measure assumes the 7d crossed 99.0 at the **midpoint** of the gap between the last reading below the mark (`at`) and the first one at it (`read_at`). With 120 s polling that error is at most ±1 min, which is fine. But `arm_time` accepts gaps up to 30 min. Such gaps occur when:

- the engine or service is paused (laptop sleep, restart);
- the account was polled at the post-429 cadence;
- a reading was skipped.

Meanwhile other use (another machine, or a long-running turn) carries the 7d across 99 early in the gap. The time rule counts from `at`, the earliest possible crossing, which is conservative. The 5h measure credits half the gap as "not yet used", which is not.

**Reproduced** with the real helpers (`_arm_five_h`, `_fold_five_h`, `policy.ride_used_5h`), at a 7d pace of 30 min per point, k = 0.165, 2-minute readings, and the 7d crossing 10 s after `at`:

| gap before arm reading | estimate when the true 7d reached 100% |
|---|---|
| 2 min | 0.96 (fine) |
| 5 min | 0.93 |
| 10 min | 0.89 |
| 20 min | **0.72**: rides into 100% with t ≈ 0.88 |

With a uniform crossing in a 20-min gap and T1 = 30 min, roughly a third of such rides hit, against the 10% design. Every one of them also pulls the shared `t` down by 0.09 for all accounts.

A 429 backoff blocks the ride only while `recent_429` holds. The arm (and its gap) persists after that, so the ride starts later on the stale midpoint.

**Fix.** Use the midpoint credit only when `read_at - at` is at most about `2 × ACTIVE_HIGH_USAGE_INTERVAL_S` (≈ 4–5 min). Otherwise either:

- count from `at` (`phase_armed = phase_5h(samples, at, …)` without the half-gap), or
- skip the 5h measure (`fiveH` absent, so the time rule applies) for that ride.

Add a test with a 20-min pre-arm gap.

#### R2 — should-fix: `t` is learned per window, but the bias it must absorb is per account (k)

`ride.py:218-240` (`learn(..., by_5h=True)` updates the global `rideLearning["7d"]["t"]`), `policy.py:601` (`ride_k` is per account), `drain.py:102-143` (`learn_k`).

`used` scales linearly with k, so a k that is off by x% shifts the share at which an account really switches by about x%. The learned k is not an unbiased estimate with ±3% *per-ride* noise, as the simulation assumes (`test_ride_controller.py:184`, `K_NOISE = 0.03`, drawn afresh each ride, with the learned k exactly equal to the mean). It is a **persistent per-account offset**, for these reasons:

- **Quantization.** It is the median of as few as 3 windows (`K_MIN_WINDOWS`). Each window is Δ7d/Δ5h with Δ5h ≥ 40, so Δ7d is about 7–16 whole points. Quantization alone gives each window ±6–15% error, so a 3-window median is easily ±5–8% off. That offset persists until enough new windows accumulate.
- **Stale history.** The history keeps 8 days (`history.POINTS_KEEP_S`). After a plan change (5x↔20x, where k moves by about 1.6×) or after a slot is re-assigned to a different login, the median mixes the old account or plan with the new one for up to a week. In the meantime `used` can be under-read by tens of percent, and almost every ride on that account hits.
- **Model mix.** Any difference in how the 5h and 7d weight different models makes the real k drift with the mix; a median hides that.

A single shared `t` then settles on the *mix* of accounts. An account whose learned k is 8% low reads `used` 8% low, so at t = 0.88 it really switches at about 0.88 / 0.92 ≈ 0.96 of the point before noise, and it hits far more than 10%. An account whose k is high stays well short. The "~10% of rides hit" promise in README/reference is per window, not per account.

**Fix (any one):**

- Key `t` (and ideally q) per account and window in `rideLearning`, falling back to the window value.
- Or require a tighter k before the 5h path is used: at least 5–6 windows, a bounded spread such as IQR/median ≤ 10%, and agreement with the plan default within, say, ±35% (this catches a plan change or slot re-use). Otherwise use the time rule.

Also make the simulation draw a **persistent** k bias per simulated account (2–3 accounts, ±5–8%) and assert the per-account hit rate.

#### R3 — should-fix (low): a multi-window `due` teaches windows that were not due or were capped

`policy.py:745-760`, `engine_hook.py:1851`.

`ok` is `decision.ride_windows` (every ridden window), and `capped` is `all(p.capped for p in due)`. This only arises with `rideWindows = "5h,7d"` (not the default). In that setting:

- the 5h can reach its learned share (not capped) while the 7d is neither due nor capped, so the 7d's `t` (or q) gets +0.01 for a share it never reached;
- if the 7d is due only by `rideMaxMin` and the 5h by its learned share, the 7d is taught although its ride was capped.

That biases the 7d controller upward, which means more hits.

**Fix.** Teach `ok` only for `w` where `p.due and not p.capped`, and carry per-window capped flags in `Switch`.

#### R4 — should-fix (low): migrated `rideLearning` records can start at the edge with the slow start

`ride.py:193-201`.

A halving-era q of 0.6 or more is kept and marked `settled: False`, so the next clean rides add +0.05. But the old q was learned against the old, much shorter T1 (`min(median, velocity)`; the docstring itself says the old rides used about a fifth of the point). A legacy q of 0.9 (likely after many clean rides, because the old T1 was short) is therefore applied to an accurate T1 and goes to 0.95 after one clean ride. That is above the simulated settle point (~0.88) and costs a burst of early hits before the controller settles.

**Fix.** Migrate to `Q_START` unconditionally (the old q is not comparable across T1 definitions), or keep `min(q, Q_START)` with the slow start ahead. Update the `test_a_halving_era_record_migrates…` case to cover a legacy q of 0.9.

#### R5 — nit: 5h reset detection by value only

`engine_hook.py:1013` (`if x.pct5 < five["p5"]`), `ride.py:498` (`rise_5h`).

A 5h reset is detected only as a drop in value. If the 5h was at 0–1% when last folded and the new window reads at least as much, the reset is missed: the old window's carry is lost, and `rise` counts only the difference. That under-reads `used`, which is the risky direction. It is rare during a ride, because a busy 7d at 99% usually has a high 5h. The 5h `resets_at` from the reading is available and makes the detection exact. Use it.

#### R6 — nit: returning to a parked, armed 7d reads the phase as a lower bound

`ride.py:489-493` (the no-steps branch of `phase_5h`), `engine_hook.py:682-697` (samples are cleared on an account change).

After a manual switch back to an account whose 7d is still armed, the new tenure has no 5h steps. `phase_now` falls back to "first reading at the current value", which is a lower bound, so it under-reads by up to about k × 0.95 ≈ 0.16 of a 7d point. If the 5h also reset while the account was parked, `carry` comes out as 0 and the old window's tail is lost. Both effects ride longer than intended. Landing logic never lands on an account at its 7d mark, so only a manual switch gets here. Consider dropping `fiveH` (falling back to the time rule) when the account was parked between folds.

#### R7 — nit: the reset-wait and the measured ride interact through `until`

`policy.py:676` and `policy.py:762`.

When neither T1 nor the 5h pace is known, `until = cap_until` (arm + 60 min). `_ride_reset_wait` then turns any 7d reset within 60 minutes into a `reset-wait` Hold. That Hold:

- clears `record["riding"]`, so a hit during it teaches nothing;
- tells the user "waiting it out";

while the `due` check (evaluated first each tick) still switches at `t`. Behavior is safe, but the label and the learning are inconsistent. The 30→60 change doubled the span where this happens.

#### R8 — nit: the measure can flip mid-ride

`policy.py:632-643`.

`ride_used_5h` returns None whenever the samples are not fresh, so a single stale tick falls back to the time rule for that tick. The time rule may say `due` (ok taught to q, not t), and the `by5h` attribution for a later hit is whatever the *last* Hold said. This is rare and safe in direction (it switches earlier), but it mixes the two controllers' evidence. Consider freezing the measure kind at arm time (`fiveH` present means always by the 5h, with a stale tick simply holding).

#### R9 — `rideMaxMin` 60: acceptable, with one caveat

`settings.py:122`.

- **5h-measured rides.** The cap is a true backstop and 60 is safe. The 5h follows real use, so a longer ride does not increase the hit exposure.
- **Time-rule rides.** These are rides without a learned k (a new install or new account), or ones that fall back per R1/R2. T1 is frozen at the arm time from steps up to 6 h old (`INTERVAL_MAX_AGE_S`). The burst override needs at least 2 points of 7d within `idleWindowMin` (10 min), which practically never happens at 7d 98–99%. So a pace that speeds up during a 50-minute ride is not seen, and the old 30-min cap used to bound that exposure.
- **The controller still contains it.** Hits push the shared q down. So this is not a blocker. Consider either keeping a 30-min effective cap for *time-rule* rides, or requiring the last timed step to be recent (for example, within 1 h) before trusting a T1 over 30 min.
- **The idle rule shortens slow rides anyway.** With `idleMaxDeltaPct` 1.0 over 10 min, a 7d point of about 55–60 min or more moves the 5h about 1 point per 10 min, so it is classified idle and switched. In practice the 30→60 change matters for 7d points of about 33–55 min. The README rationale ("a ride that would run longer is mostly one that slowed to a pause") holds.

#### R10 — design note, not a defect: a 10% hit rate means one interrupted turn per ten rides

At 100%, Claude Code's next request fails and the running turn stops. The README describes a hit as costing "one refused request", but for an agentic turn it can mean a stopped turn that the user must resume. That is about one per account every ~10 weeks on 7d, or every couple of weeks across 5 accounts.

This is an intentional trade (≈ 99.9% use). The docs should say plainly that a hit stops the running turn. They should also point to `maximize.learnedRide false` or `rideWindows ""` for users who prefer never to be interrupted. `TARGET_SHARE = 0.9` is shown as "target ~0.9" next to a t of 0.88, which reads as if t is the target. Reword it as "aims for ~1 hit in 10".

#### R11 — nit: always-on history writes

`engine_hook.py:786`.

With the defaults (`rideWindows=7d`) the usage-history points are now recorded even when the user has turned off both `preempt` and `drainHours`. That adds disk writes and creates a history file they did not have before. Mention it in the reference table under `rideWindows`.

### Test realism (`tests/maximize/test_ride_controller.py`)

Good: the simulation drives the real `observe` / `point_seconds` / `point_estimate` / `_arm_five_h` / `_fold_five_h` / `ride_used_5h` / `learn` code, and it is seeded and deterministic. It is optimistic in ways that matter for the 10% claim:

1. **k error is iid per ride, and the learned k equals the true mean** (`K_NOISE = 0.03`, `k7={"1": K7}`). The real error is a persistent per-account bias of ±5–10%, and multiple accounts share one `t` (R2). The headline "~10% hits, ~0.91 of the point" is the best case.
2. **Polling is perfect** (120 s ±10%, never missed, no stale tick, no 429), so the pre-arm gap is always ≤ 2.2 min. R1 is never exercised.
3. **Usage is continuous and exactly proportional** (`level5 = (level7 − start7)/k_true`). Real use is lumpy: requests land in bursts, which hurts the 5h phase extrapolation (`phase_5h`'s line fit). It also never pauses short of the idle rule, and the 5h and 7d may weight models differently.
4. **One account, the 7d only.** There is no multi-window ride (R3), no migration from a high legacy q (R4), no 5h reset near 0% (R5), and no parked return (R6).
5. **Floor rounding is assumed.** The whole design rests on the usage endpoint reporting `floor(pct)`. If it ever rounds to nearest, "99" means 98.5–99.5 and every share is off by half a point. Nothing pins this down. A comment or a test fixture with a captured real response would help.

I suggest adding:

- a persistent per-account k bias with 2–3 accounts, asserting the per-account hit rate is ≤ 15%;
- a test with a 20-min pre-arm gap (R1);
- a multi-window `ok` attribution test (R3);
- a test for a legacy q of 0.9 (R4).

### Verdict: `feat/ride-to-99-9`

**Not mergeable as is.** R1 and R2 can each push real hit rates well above the ~10% the docs promise:

- R1: about a third of rides armed after a 20-min gap;
- R2: an account with a biased or stale k hits on most rides.

Each fix is small and local:

- R1: gate the midpoint credit on the gap.
- R2: per-account `t`, or a stricter learned-k acceptance.

R3 and R4 are one-line-scale fixes and should go in the same push. R5–R11 can follow. With R1 and R2 fixed (and the simulation extended to cover them), I would approve.

---

## 2. `fix/doctor-service-stale-race` (`2c9c54d`)

`src/claude_swap/maximize/doctor.py:1205-1220`, `:1291`.

The tolerance between the install mtime (nanosecond precision) and the process start (whole seconds; on Linux derived from boot time plus clock ticks) is raised from 1 s to 5 s and factored into `_started_before_install`. Doctor now no longer flags a service restarted by `service install` immediately after `uv tool install --force` as running old code.

- **Correctness.** `service install` restarts right after writing, so a real stale process is minutes to days old. A 5 s window cannot hide one, except a process that happened to start in the 5 s *before* an upgrade without a later restart. That is negligible, and `doctor` would flag it on the next upgrade.
- **None handling** is preserved: an unknown start or install time is not stale.
- **Tests** cover the same-second case, lags of 0–3.9 s (not stale), 6 s / 60 s / 1 h (stale), and unknowns. All 78 doctor tests pass.
- **Nit.** The parametrised boundary stops at 3.9 / 6.0. A test at exactly 5.0 would pin the `<` versus `<=` choice. This is optional.

**Mergeable: yes.**

---

## 3. `chore/stale-urgent-comments` (`ee87e0b`)

Comment and test-comment wording only:

- `usage_store.py:1039`: "bounded urgent cadence" becomes "120 s high-usage cadence". This matches `poll_policy.ACTIVE_HIGH_USAGE_INTERVAL_S = 120.0`.
- `test_engine_maximize.py:107,111`: "urgent mode" becomes "poll threshold".
- `test_switcher.py:1796`: "urgent cadence" becomes "sub-floor cadence". The plan in that test is 60 s, below `MIN_INTERVAL_S = 180`, so "sub-floor" is accurate.

There are no code changes. The tests touched show only the root-environment failures that main also has. "urgent" legitimately remains in `engine_hook` (`RESET_WAIT_URGENT_S`, `urgent=`), where it names the reset-wait cadence, so leaving those alone is right.

**Mergeable: yes.**

---

## Fix round

`feat/ride-to-99-9` @ `04e222e` (`04e222e1578c64ed0ae49c50eda89f02a7acc254`), one commit on top of `710f4b8`. It fixes R1–R5 and R10. R6–R9 and R11 are unchanged.

### What changed

- **R1, the pre-arm gap.** `engine_hook._arm_five_h` credits the midpoint only when `read_at - at <= ride.MIDPOINT_MAX_GAP_S`. That constant is 240 s, twice `ACTIVE_HIGH_USAGE_INTERVAL_S`. Over a longer gap the count starts from `at`, the same way the time rule counts.
- **R2, both fixes from the review.**
  - **Per-account learning.** `rideLearning["accounts"][slot][window]` now holds only the fields that account learned itself: `t` from a ride measured on the 5h, `q`/`settled` from a timed ride, plus the account's own counts. Every other field falls back to the window's record. Each ride teaches both the account and the window. The window record is what an account with no record of its own uses. The new helpers are `ride.learned_for` and `ride.learned_accounts`. `q_values` and `t_values` take an optional slot. The engine (`_ride_track`, `_ride_commit`) and Fleet (`view._own`) read and write per account. Doctor and `why` list each account's own t.
  - **Stricter k for the 5h path.** A new function, `drain.ride_k`, accepts k only from at least 5 windows (`K_RIDE_MIN_WINDOWS`) with IQR/median of at most 10% (`K_RIDE_MAX_SPREAD`). It lands in the new `Snapshot.ride_k7`. `policy.ride_k` also requires k to be within ±35% (`K_RIDE_PLAN_BAND`) of `drain.fallback_k(plan)`: 0.165 for 20x and unknown plans, 0.105 for 5x. If any check fails, the time rule rides. `learn_k` (3 windows) still feeds the drain unchanged.
- **R3, multi-window attribution.** A new field, `Switch.ride_ok`, lists the windows where `p.due and not p.capped`. The engine teaches `ok` and `ok_5h` only for those windows.
- **R4, legacy records.** A halving-era record (no `"v"`) now migrates to `Q_START` whatever its q, with the slow start still ahead.
- **R5, 5h reset detection.** The 5h measure stores the 5h `resets_at` of the last reading (`fiveH.r5`), but only when it is ahead of that reading. `_fold_five_h` counts a 5h reset when a reading is taken at or after it, or when the value drops. The carry is taken at the old window's `resets_at` when that is known, otherwise at the midpoint. `ride.rise_5h` takes an explicit `reset=` argument.
- **R10, docs.**
  - README and `docs/reference.md` now say that a hit stops the turn Claude Code was running.
  - They say the ride "aims for ~1 hit in 10" (roughly one interrupted turn per account every ten weeks on 7d).
  - They point to `maximize.learnedRide false` or `maximize.rideWindows ""` for users who want never to be interrupted.
  - The doctor/why line says `(aims for ~1 hit in 10, …)` instead of `target ~0.9`. `TARGET_SHARE` is removed.
  - The `ride` row in `doctor_cli.REASONS` matches the reference table.
- **Docs for R1, R2, R4 and R5.** The reference describes the stricter k, the 4-minute midpoint gate, detection by `resets_at`, per-account t/q and the new migration.

### Simulation (`tests/maximize/test_ride_controller.py`, seeded)

| Scenario | Hits | Share of last point | Settled value |
|---|---|---|---|
| 5h measure, single account (seeds 1/2/3) | 9.6% / 9.8% / 9.8% | 0.915 / 0.913 / 0.914 | t 0.890 / 0.888 / 0.887 |
| Time rule, q (seeds 1/2/3) | 8.9% / 8.8% / 9.4% | 0.808 / 0.813 / 0.803 | q 0.874 / 0.877 / 0.866 |
| Legacy q 0.9 record (seed 6) | 1 hit in the first 10 rides | | starts at 0.60, settles at q 0.884 |

**Persistent per-account k bias.** Learned k is off from the real k by a fixed bias per account, with ±3% noise per ride on top. Seed 5, 200 burn-in rides, then 500 rides per account:

| Biases | Account (bias) | Own t: hits / share / t | One shared t: hits / share / t |
|---|---|---|---|
| −8%, +5% | 1 (−8%) | **10.0%** / 0.911 / 0.816 | 19.6% / 0.931 / 0.840 |
| | 2 (+5%) | **7.2%** / 0.915 / 0.933 | 0.4% / 0.817 / 0.830 |
| −8%, 0, +6% | 1 (−8%) | **9.8%** / 0.907 / 0.810 | 26.6% / 0.943 / 0.851 |
| | 2 (0) | **10.2%** / 0.912 / 0.882 | 2.6% / 0.862 / 0.835 |
| | 3 (+6%) | **6.4%** / 0.906 / 0.934 | 0.6% / 0.826 / 0.842 |

With its own t, every account stays at or below about 10% hits (the test bound is ≤ 15%) and uses about 0.91 of the point. With one shared t, the account whose k reads low hits on 20–27% of its rides.

**R1, pre-arm gap.** 300 rides at t fixed to 0.88, a 7d pace of 30–45 minutes per point, and the 7d crossing 99 uniformly within the gap:

| Gap | Before: midpoint always credited | After: gated at 240 s |
|---|---|---|
| 10 min | 25.3% hits, share 0.911 | **1.0%** hits, share 0.772 |
| 20 min | 34.3% hits, share 0.886 | **2.7%** hits, share 0.659 |

A ride after a long gap now gives up part of the point instead of riding into 100%.

### Tests

- **`test_ride_controller.py`.** It went from 12 to 32 tests. The new ones cover:
  - persistent per-account k bias with 2 and 3 accounts, asserting each account's hit rate is ≤ 15% and its share ≥ 0.88;
  - a shared-t contrast case, where one account exceeds 15%;
  - fallback from account to window;
  - `drain.ride_k` window count and spread, and the plan band (8 cases);
  - the 240 s midpoint gate, and pre-arm gaps of 10 and 20 minutes (≤ 5% hits now, ≥ 20% with the old rule patched back in);
  - multi-window `ride_ok` when the other window is not due, is due only by the cap, or both are capped;
  - migration from a legacy q of 0.9;
  - a 5h reset near 0% with no drop, caught by `resets_at`. Value-only detection counts nothing there.

  The existing simulations now feed `ride_k7` and the 5h `resets_at`.
- **Updated tests.**
  - `test_ride_learning.py`: migration is parametrised over legacy q 0.85, 0.9 and 0.95; `fiveH` gains `r5`; the engine test asserts the per-account record; `drain.ride_k` is monkeypatched.
  - `test_ride_policy.py`: `ride_k7`.
  - `test_ride_surfaces.py`: the "aims for" wording.
- **Results of `uv run pytest -q` as root.**

  | Branch | Failed | Passed | Skipped |
  |---|---|---|---|
  | `04e222e` | 38 | 5651 | 31 |
  | `710f4b8` (before) | 38 | 5629 | 31 |
  | main | 38 | 5584 | 31 |

  The failing set is identical on all three, and every failure is the CLI's "Do not run this script as root" refusal: `test_login_new` (5), `test_login_upsert` (8), `test_names` (1), `test_prime_auto_verify` (1), `test_prime_cli` (7), `test_prime_verify` (2), `test_relogin` (4), `test_cli` (5), `test_switch_degraded_read` (1), `test_switcher` (2) and `test_tui` (2).

  `tests/maximize` alone: 28 failed and 2866 passed, against 28 failed and 2799 passed on main.
- **Lint.** `ruff check` on `src/claude_swap/maximize` reports only the 2 findings already on the base, in `live_repair.py` and `policy.py` (an unused `poll_policy` import).

### Verdict update

R1 and R2 are fixed and covered by simulation, and R3–R5 and R10 are in. **`feat/ride-to-99-9` @ `04e222e` is mergeable** from this review's side. R6 (a parked return reads the phase as a lower bound), R7 (the reset-wait label and learning), R8 (freezing the measure kind at arm time), R9 (an effective cap for time-rule rides) and R11 (documenting the history writes) remain open as follow-ups.

---

## Verification of 04e222e

This is an independent check of `feat/ride-to-99-9` @ `04e222e` against R1–R5 and R10. It does not rely on the "Fix round" summary above. No code was changed. Line numbers are at `04e222e`.

**Tests, re-run as root.** `tests/maximize`: 28 failed, 2866 passed. The 28 failures are the same root-refusal set as on main (`test_login_new` 5, `test_login_upsert` 8, `test_names` 1, `test_prime_auto_verify` 1, `test_prime_cli` 7, `test_prime_verify` 2, `test_relogin` 4). The four ride test files pass (152 passed). `ruff check src/claude_swap/maximize` shows only the 2 findings already on the base.

### Status per finding

| # | Status | Evidence |
|---|---|---|
| R1 | **Fixed** | `engine_hook.py:1010`: the half-gap is credited only when `read_at - at <= ride.MIDPOINT_MAX_GAP_S` (240 s, `ride.py:170`). Otherwise the phase is `phase_5h(samples, at, …)`, which counts from the arm time, the conservative end. The 120 s ±10% cadence stays inside the gate. A single missed poll (≈ 240–264 s) can fall just outside it and lose the credit. That is a small cost in the safe direction. |
| R2 | **Fixed** (residuals below) | Per-account learning: `ride.py:200` (`_item`, field-by-field fallback), `ride.py:278` (`learned_for`), `ride.py:320-360` (`learn(..., account=)`, which steps the account record and the window record). The engine reads the active account's values (`engine_hook.py:1187-1188`) and teaches the slot that rode (`engine_hook.py:1235`, where `record["account"]` is the pre-switch account). Fleet does the same (`view.py:463,467`). Stricter k: `drain.py:158` (`ride_k`: ≥ 5 windows, IQR/median ≤ 0.10), `policy.py:604-620` (±35% band). It is fed only while the 7d rides (`engine_hook.py:812`). |
| R3 | **Fixed** | `policy.py:764` (`ok` = due and not capped), carried in `Switch.ride_ok` (`policy.py:775`). The engine teaches only `ride_ok`, and the 5h subset of it (`engine_hook.py:1883-1884`). When every due window is capped, `ride_ok` is empty, so the old `not ride_capped` guard is kept. |
| R4 | **Fixed** | `ride.py:208`: a record with no `"v"` gets `Q_START` whatever its q. The slow start stays ahead (`settled` requires `current`). The test covers legacy q 0.85, 0.9 and 0.95. |
| R5 | **Fixed** | `engine_hook.py:1033`: a reset is a drop in value, or a reading at or after the stored `r5`. The carry is taken at `r5` when it falls inside the gap. `r5` is cleared on consumption (`:1041`) and refreshed from the current reading (`:1047`). A false positive (a `resets_at` reported early) adds the whole new reading. That over-reads `used` and switches early, which is the safe direction. |
| R10 | **Fixed** | The README, the `docs/reference.md` "Riding the last point" paragraph, and the `ride` row in reference and `doctor_cli.REASONS` now say that a hit stops the running turn. They say the ride aims for ~1 hit in 10, and they name `learnedRide false` / `rideWindows ""`. `TARGET_SHARE` is removed. `describe` says "aims for ~1 hit in 10". |

### Regression checks

- **State file and upgrade from v0.5.9.**
  - The new data sits under `rideLearning["accounts"]`. `learned()` iterates only `WINDOWS`, so code that does not know the key ignores it.
  - A v0.5.9 halving-era record migrates on read, and nothing is written until the next learn.
  - An armed v0.5.9 entry has no `fiveH`, so the ride in flight rides by the time rule. A `fiveH` from `710f4b8` without `r5` falls back to detection by value.
  - Downgrading drops the per-account part harmlessly.
- **Size.** Per slot it holds at most 2 windows × 7 small fields. It is bounded by the number of slots. Records of removed slots are never pruned (nit).
- **Fallback when k is rejected.** `ride_used_5h` returns None, and the ride uses the time rule with the account's own q. When T1 is also unknown, the switch is immediate ("pace unknown"), as before. Rides do not stop starting.
  - I simulated `drain.ride_k` on quantized history: Δ5h 40–100 per window, whole-percent readings, and real-k variation σ of 0, 3% or 6% per window.
  - Acceptance was 97% / 93–95% / 70–77% respectively, for 5–10 windows.
  - The accepted k had an error sd of 1–3%, with a tail of 5–13%.
  - So the stricter rule rarely disables the 5h path, and it bounds the iid part of the k error as intended.
- **Riding into 100% more than intended.** I found no new path. The midpoint gate, k rejection and the R3 attribution all move in the conservative direction. Two residual paths are left. Both are rare and both were already present in some form:
  - **The plan band sits on the wrong side for the dangerous direction.**
    - The band is ±35% around 0.165, so its lower bound is 0.107. A 5x account's real k of about 0.11–0.12 therefore *passes* for a 20x account.
    - In simulation, a slot whose history is still 7–8 windows of the 5x k (or of a different login with k 0.15 on a slot now holding k 0.18) was accepted in 62–95% of draws. Those k values under-read `used` by 15–33%, so a ride there would very likely hit.
    - The IQR check catches the mix only once 2–3 new windows exist.
    - After a real upgrade the 7d needs many new windows before it reaches 99%, so this is mostly the case of a slot re-used for a login already near its weekly limit.
    - The per-account t of the old login also carries over to the new login.
  - **The measure can flip mid-ride.** `ride_k7` is recomputed every tick, including the ongoing 5h window, so the k check can flip mid-ride. This extends R8: it mixes q and t evidence, and the direction is safe.
- **`describe` nit (verified).** For an account that has learned only q (timed rides), the "per account" list shows the window's t as if it were the account's own. Its own q is never shown. Example: `learn(None, "7d", "ok", 1, account="2")` → `per account: 2 0.85`.

### Are the new simulations honest?

- **Persistent per-account k bias (`simulate_accounts`).** The test is honest about what it tests: a fixed bias per account, ±3% iid noise per ride on top, and the real `ride.learn` / `t_values` with `account=`.
  - It measures only the steady state: 200 burn-in rides per account, which is years at about one ride per week.
  - I ran the transient. A −8% account joins after the window t has settled on an unbiased account. It hits on 46% of its first ride and 24% over its first 5 rides, then 9.9% from ride 6 on: about 2.7 hits in its first 20 rides, against 2.0 at a flat 10%. That is a one-time cost of under one extra interrupted turn. It is acceptable, but the docs' "each account ~6–10%" is the long-run figure.
  - The learned k is still injected, not derived from simulated history. The test does not show that `drain.ride_k` produces a bias of that size or persistence; my `ride_k` simulation above covers part of that gap.
- **20-minute pre-arm gap (`test_a_long_gap_before_the_arm_does_not_ride_into_100`).** This test is honest and worst-case.
  - The crossing is uniform within the gap.
  - Use continues at full pace through the gap (another machine), and the 5h rise across the gap is folded in.
  - With the gate patched out (`MIDPOINT_MAX_GAP_S = inf`) the same seed reproduces the ≥ 20% hits.
  - It runs at a fixed t of 0.88 with a pace of 30–45 min per point. A faster pace makes the gap matter less, because the gap-length credit becomes a smaller share of the point.
- **R3, R4 and R5 tests.** They exercise the real `policy.decide`, `simulate(q0=legacy)` and `_fold_five_h` with a reset near 0% and no drop. They are adequate.

### Verdict

**Mergeable: yes.** I found no blockers. R1, R3, R4, R5 and R10 are fixed. R2 is fixed for the cases the review raised: quantization bias, and a shared t that settles on the mix.

Should-fix, in this PR or as an immediate follow-up:

1. **Make the k plan band asymmetric** in `policy.py:618`, so it rejects a k below the plan default by more than about 20%. Alternatively, drop history points and the slot's `rideLearning["accounts"]` entry when a slot's login or plan changes. As it stands, a 5x-era k passes for a 20x account and under-reads the last point by about a third.

Nits and follow-ups:

- the `describe` per-account list (show t only for accounts with an own t, and q for those with an own q);
- prune per-account records of removed slots;
- the docs' per-account hit rates are steady-state;
- R6–R9 and R11 are still open, as listed above.
