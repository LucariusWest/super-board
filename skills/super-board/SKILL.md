---
name: super-board
description: GitHub-Project-driven autonomous pipeline. Six verbs — onboard, lint, status, run, stop, sentry — that take a Project board from empty to drained across Build → QA → Review → Done lanes, with graceful shutdown / resume and an attached event watcher. Use when the user says "super-board", "/super-board", "drain my GitHub project", "set up the autonomous loop", "kick off the headless build/QA pipeline", "watch the board", or "stop super-board".
---

# super-board — autonomous GitHub Project pipeline

Spec: `docs/superpowers/specs/2026-05-21-super-board-design.md`

## Six verbs

| Verb                  | Where                   | What it does                                                                                                                                                                                                    |
| --------------------- | ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `super-board onboard` | interactive             | one-time setup wizard; writes `.claude/super-board/configs/<slug>.json`                                                                                                                                         |
| `super-board lint`    | interactive             | walks active-pipeline issues, flags vague ACs, runs pre-flight readiness                                                                                                                                        |
| `super-board status`  | interactive (read-only) | snapshot of active config, column counts, in-flight workers                                                                                                                                                     |
| `super-board run`     | headless                | the autonomous loop; spawned via `.claude/bin/super-board-run.sh`. Also the resume command — state lives on the board, not in process memory.                                                                   |
| `super-board stop`    | interactive             | graceful shutdown: posts "stopped mid-flight" comments on every in-flight issue + PR, releases assignee mutexes, kills workers + dispatcher. Next `super-board run` resumes.                                    |
| `super-board sentry`  | interactive (read-only) | long-running watcher in the current session. Polls every 2 min via `ScheduleWakeup`; emits inline alert blocks on real events and a full status snapshot every 15 min. Optional Telegram fan-out. Ctrl-C exits. |

If invoked with no verb, ask which (see no-verb behavior in spec §8).

## Routing

| If user says                                                                      | Load                                                                           |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `super-board onboard ...`                                                         | `references/onboard.md`                                                        |
| `super-board lint ...`                                                            | `references/lint.md`                                                           |
| `super-board status ...`                                                          | `references/status.md`                                                         |
| `super-board run ...`                                                             | `references/run.md`                                                            |
| `super-board stop ...` / "stop the run" / "pause the loop" / "kill super-board"   | `references/stop.md`                                                           |
| "resume" / "pick up where I left off" / "restart after stop"                      | `references/stop.md` (resume = run; no separate verb)                          |
| `super-board sentry ...` / "watch the board" / "tail super-board" / "sentry mode" | `references/sentry.md`                                                         |
| Anything about Block/Skip exits                                                   | `references/block-template.md`                                                 |
| Config structure questions                                                        | `references/config-schema.json`                                                |
| Worker gh-call discipline / rate-limit recovery                                   | `references/rate-limit-etiquette.md` (+ `.claude/bin/super-board-gh-guard.sh`) |

Replaces: `super-work-trader` (rename + extension). The 3-lane mechanics are inherited; the front door (onboard / lint / status / stop) is new.

## Orchestrator vs worker — the cardinal rule

super-board is an **autonomous trader**. The interactive Claude session that invokes any of the six verbs is an **orchestrator**, not a worker. The orchestrator:

- Validates preconditions, dispatches `nohup ./.claude/bin/super-board-run.sh` (for `run`), reports PID + log path, exits.
- Delegates all build / QA / review work to headless `claude -p` workers spawned by the dispatcher.
- Must NOT do product work itself, must NOT patch the dispatcher mid-run, must NOT wait for workers, must NOT hold context for multi-card progress. `sentry` is the one read-only exception that stays attached — but it never touches workers, only polls + renders.

If anything goes wrong during a run, the orchestrator captures the symptom and reports back — it does not silently expand the task into a fix. See `references/run.md` "Orchestrator delegation contract" for the full rule.
