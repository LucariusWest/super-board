# `super-board sentry` — long-running pipeline watcher

> **Source of truth:** this file. The sentry verb was added 2026-05-26
> after a brainstorming pass; no separate spec section in
> `docs/superpowers/specs/2026-05-21-super-board-design.md` yet — fold
> one in there if it ever gets cross-referenced from another skill.
>
> **Read-only contract.** Like `status`, sentry must never mutate GitHub.
> Forbidden-call set is identical — see "Worker self-check" at the bottom of
> this file. Telegram fan-out via `mcp__plugin_telegram_telegram__reply` is
> not a GitHub mutation and is allowed.

---

## What sentry is

A two-minute polling loop that keeps the current Claude session attached to
the board and surfaces only the events that matter:

- **Inline alert blocks** when something happens — card moved column, worker
  dispatched, worker reaped, PR merged, run started/stopped, block-rate
  alert, sentry-side gh outage.
- **Full status snapshot every 15 minutes** as a rolling anchor — same output
  as `.claude/bin/super-board-status.py`.

Quiet ticks emit nothing. Pure signal.

## Where it runs

Interactive — same Claude session that invoked `/super-board sentry`. The
loop is driven by `ScheduleWakeup` (one schedule per tick) so the cache
stays warm under the 5-min TTL. There is **no headless dispatcher process**;
sentry is the orchestrator, not a forked worker.

The tick work — manifest delta parsing, column-count diff, alert rendering —
is delegated to `.claude/bin/super-board-sentry.py` (added 2026-05-26, pure
Python stdlib + `gh`, same renderer shape as `super-board-status.py`).

## Intro shown when sentry starts

```
👀 super-board sentry
─────────────────────────────────────────────────────────
Watching the board. Alerts fire on real events; full status
snapshot every 15 min. Ctrl-C / ESC to stop.
─────────────────────────────────────────────────────────
```

## Entry sequence — every sentry invocation

The orchestrator MUST run this sequence on EVERY `super-board sentry`
invocation, in order. Don't shortcut steps even when resuming.

**Step 0 — invocation routing.** Based on the args:

- **`--tick` flag** (set by every `ScheduleWakeup` re-fire) → SKIP the
  entire entry sequence. Jump straight to "Per-tick behavior" below.
- **`--reconfigure` flag** → run the menu (Step 1) regardless of current
  config state, then continue through steps 2–5.
- **No flags + missing `sentry` block in active config JSON** → first
  run. Run Step 1, then continue.
- **No flags + `sentry` block present** → resume. Skip Step 1, continue
  from Step 2.

The first-run trigger is **"no `sentry` block in the active config"**,
not "no state file". State files come and go (Ctrl-C resets, manual
deletes); the config block is the durable "user has been onboarded to
sentry" marker.

**Step 1 — interactive menu (first-run / reconfigure only):**

Run an `AskUserQuestion` flow with TWO questions (in one call):

- "Mirror sentry alerts to Telegram?" → `Yes` / `No`
- "Which alert types should fan out to Telegram?" (multi-select) →
  `MERGED`, `BLOCKED`, `DISPATCH`, `REAP`, `ZOMBIE`, `BLOCK-RATE`,
  `DONE`, `SKIPPED`, `RUN-START-STOP`. Only ask this if the first answer
  is `Yes`.

Persist the answers into the active config under a new `sentry` block:

```json
"sentry": {
  "tick_minutes": 2,
  "heartbeat_minutes": 15,
  "telegram_alerts": ["merged", "blocked", "block-rate"]
}
```

Use `jq` to merge — do not rewrite the whole file. Lowercase the alert
keys (the script matches them lowercase): `merged`, `blocked`, `dispatch`,
`reap`, `zombie`, `block-rate`, `done`, `skipped`, `run-start-stop`. Any
key not in this list will never fan out — keep this set in sync with the
`tg_key` returns inside `render_event` in `super-board-sentry.py`.

**Step 2 — baseline status snapshot (EVERY entry, not just first-run):**

Print `python .claude/bin/super-board-status.py <slug>` stdout verbatim. This is
your anchor on every sentry entry — first-run, resume after Ctrl-C, or
`--reconfigure`. Don't skip it; the user opening sentry mode always wants
to see the board state right now, not wait up to 15 min for the first
heartbeat.

**Step 3 — state file init or load:**

- State file missing → `python .claude/bin/super-board-sentry.py --first-run <slug>`
  seeds it. The script is silent in this mode (just emits
  `__NEXT_TICK_SECONDS__`).
- State file present → no init call needed; the next tick will read it.

**Step 4 — "Sentry armed" banner:** print the block from §"Intro shown
when sentry starts" above.

**Step 5 — schedule the first tick:**
`ScheduleWakeup(delaySeconds=120, prompt="/super-board sentry --tick", reason="sentry tick — watching board")`.

The `--tick` suffix is the disambiguation marker between a user-typed
fresh invocation (`/super-board sentry`) and a scheduled wakeup
(`/super-board sentry --tick`). The fresh form re-runs the entry sequence
above; the `--tick` form skips straight to "Per-tick behavior" below.
This is why we don't re-render the full status board on every 2-minute
wake — only on actual user-initiated entry.

To re-configure later, the user deletes the `sentry` block from the config
or runs `super-board sentry --reconfigure` (re-runs the menu without
resetting the state file).

## Per-tick behavior (scheduled wakeups, `--tick` form)

Every ScheduleWakeup re-fires `/super-board sentry --tick`. The
orchestrator on each tick:

1. Verifies the active config still exists. If not, halt with the same
   error as `status`: `Run super-board onboard first.`
2. Runs `python .claude/bin/super-board-sentry.py <slug>`.
3. Parses stdout line-by-line:
   - Lines starting with `TELEGRAM:<event_key>:<message>` — strip the
     prefix, **un-escape `\n` back to real newlines** (the script escapes
     them so each Telegram payload fits on one line of the output
     protocol), then post the message via
     `mcp__plugin_telegram_telegram__reply` to the configured Telegram
     channel. Do NOT print these to the user terminal. The MERGED alert
     in particular relies on this — its payload is `title\nPR url`, and
     skipping the unescape will land a literal `\n` in the chat.
   - Line starting with `__NEXT_TICK_SECONDS__:<N>` — capture `<N>` for
     the next `ScheduleWakeup` delay. Do NOT print.
   - All other lines — print verbatim, in order.
4. `ScheduleWakeup(delaySeconds=<N>, prompt="/super-board sentry --tick", reason="sentry tick — watching board")`.

The orchestrator's output to the user per tick is ONLY the alert blocks
and (when due) the heartbeat snapshot. No commentary, no preamble, no
"checking the board now…" lines. The locked terminal aesthetic is what
the user sees.

## Telegram fan-out details

When the user enables Telegram in step 1 of first-run, sentry posts ONLY
the alert types they selected. The script emits the candidate messages on
TELEGRAM: lines; the orchestrator filters them out of the terminal stream
and posts them.

If the Telegram MCP call fails, the orchestrator should:

- Print one inline warning the first time it fails per session:
  `⚠ sentry: Telegram fan-out failed — continuing terminal-only`
- Continue the loop. Don't retry indefinitely; the next event will try
  again.

## Stopping sentry

The user presses Ctrl-C / ESC to break the loop — this is the documented
stop signal. The state file persists, so re-running `super-board sentry`
picks up cleanly from the last seen manifest offset and column state. No
separate stop verb is needed.

To reset sentry's memory (e.g. after a long pause), the user deletes
`.claude/super-board/sentry-state.json` and re-runs `super-board sentry`.

## Edge cases

| Situation                                         | Behavior                                                                                                                                                                                                          |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `super-board run` not active                      | Heartbeat snapshot still works (super-board-status.py handles §H — "no active run" itself). Alert ticks stay quiet until a run starts.                                                                            |
| Manifest missing for today                        | Treated as offset 0. Nothing to parse until the first manifest line lands.                                                                                                                                        |
| Date rollover at midnight                         | The script detects the filename change, resets `last_manifest_byte_offset` to 0, reads the new file from the start.                                                                                               |
| `gh` call fails                                   | Script catches it, increments `gh_failure_streak` in the state file. On the 3rd consecutive failure, a one-time `⚠ SENTRY: GH UNAVAILABLE` alert prints. Successful tick resets the streak.                       |
| `super-board stop` invoked in another terminal    | Sentry sees the `STOP` manifest lines, emits a `⏹ RUN STOPPED` alert, keeps watching. Next `super-board run` fires a `▶ RUN STARTED` alert.                                                                       |
| `super-board status` invoked manually elsewhere   | Zero interference. Both verbs are pure-read.                                                                                                                                                                      |
| Card moved to Done in the same tick as a PR merge | Deduped — the merge alert wins. The script suppresses the column-done alert when the same issue number appears in a merged PR alert this tick (via `Closes #N` body parsing).                                     |
| Multiple consecutive STOP lines in the manifest   | Collapsed to one `⏹ RUN STOPPED` alert.                                                                                                                                                                           |
| Telegram MCP not configured                       | Script still emits TELEGRAM: lines if `sentry.telegram_alerts` is non-empty. Orchestrator should detect missing channel config and print the same `⚠ Telegram fan-out failed` warning once. Don't crash the loop. |
| User interrupts mid-tick                          | Standard /loop interrupt behavior — the next `ScheduleWakeup` never fires. State file remains valid for resume.                                                                                                   |

## Skill routing additions

Already merged into `SKILL.md`:

| If user says                                                                      | Load                               |
| --------------------------------------------------------------------------------- | ---------------------------------- |
| `super-board sentry ...` / "watch the board" / "tail super-board" / "sentry mode" | `references/sentry.md` (this file) |

---

## Worker self-check (MANDATORY before exit)

Sentry inherits the `status` read-only contract. Before the orchestrator
returns control, confirm no `gh` invocation issued by this verb belongs to
the mutation set below. If any forbidden call was made, halt immediately
and report a contract violation; do not continue scheduling ticks.

Forbidden in `sentry`:

- `gh ... edit` (e.g. `gh issue edit`, `gh project item-edit`, `gh pr edit`,
  `gh label edit`, `gh repo edit`)
- `gh ... create` (e.g. `gh issue create`, `gh pr create`,
  `gh project item-create`, `gh label create`, `gh release create`)
- `gh ... delete` (e.g. `gh issue delete`, `gh project item-delete`,
  `gh label delete`, `gh repo delete`)
- `gh issue ... add-label` / `gh issue ... remove-label`
- `gh issue ... add-assignee` / `gh issue ... remove-assignee`
- `gh api graphql` invocations with mutations such as `resolveReviewThread`,
  `addProjectV2ItemById`, `updateProjectV2ItemFieldValue`, `closeIssue`,
  `mergePullRequest`, etc.

Allowed in `sentry` (read-only + local state + Telegram MCP):

- `gh project item-list` / `gh project view` / `gh api graphql` (queries only)
- `gh issue list` / `gh issue view`
- `gh pr list` / `gh pr view`
- Local filesystem reads of the config JSON and run-manifest markdown
- Local filesystem **write** of `.claude/super-board/sentry-state.json`
  only — this is sentry's private memory, not a GitHub mutation
- `mcp__plugin_telegram_telegram__reply` calls for opt-in fan-out

If implementation accidentally calls a mutation, halt and report:
`super-board sentry: contract violation — mutation attempted from a read-only verb.`
