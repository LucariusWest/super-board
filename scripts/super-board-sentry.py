#!/usr/bin/env python3
"""super-board-sentry.py — one tick of the super-board sentry watcher.

Called by the orchestrator (interactive Claude session) once every
`tick_minutes` (default 2). Reads the run manifest delta, fetches column
counts, diffs against the last-seen state file, and prints zero or more
alert blocks plus an optional N-minute heartbeat snapshot.

Read-only contract: same forbidden set as `super-board status`. No
mutations, no labels, no assignee writes. State file write is local FS.

Output protocol — every line goes to stdout. The orchestrator prints
everything verbatim EXCEPT lines beginning with one of these prefixes:

  TELEGRAM:<event_type>:<message>    queue a Telegram fan-out (filtered
                                     by config.sentry.telegram_alerts)
  __NEXT_TICK_SECONDS__:<N>          tell the orchestrator how long to
                                     sleep before the next tick

Cross-platform: pure Python 3 stdlib + `gh` CLI. Works on macOS, Linux,
Windows (PowerShell / CMD / Git Bash / WSL). No bash, no jq.

Usage:
  python .claude/bin/super-board-sentry.py [--first-run] [<config-slug>]

Exit codes:
  0  ok (output may or may not contain alerts)
  64 missing arg + no active marker + no single config
  66 config not found
  67 gh / network failure (3rd consecutive)
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

# Cap on `seen_merged_prs` retention. `gh pr list --limit 10` only returns
# the 10 most-recent merges, so anything beyond ~50 is dead weight — keeping
# the file from growing unboundedly over months of operation.
SEEN_MERGED_RETAIN = 50

# Make box-drawing chars render on Windows consoles too.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


# ───────────────────────────── args + paths ─────────────────────────────

argv = list(sys.argv[1:])
first_run = False
if argv and argv[0] == "--first-run":
    first_run = True
    argv.pop(0)

CONFIG_SLUG: str
if argv and argv[0]:
    CONFIG_SLUG = argv[0]
else:
    active = Path(".claude/super-board/active")
    if active.is_file():
        CONFIG_SLUG = active.read_text().strip()
    else:
        cfgs = sorted(Path(".claude/super-board/configs").glob("*.json"))
        if len(cfgs) == 1:
            CONFIG_SLUG = cfgs[0].stem
        else:
            print(f"usage: {sys.argv[0]} [--first-run] <config-slug>", file=sys.stderr)
            sys.exit(64)

CONFIG_PATH = Path(f".claude/super-board/configs/{CONFIG_SLUG}.json")
if not CONFIG_PATH.is_file():
    print(f"config not found: {CONFIG_PATH}", file=sys.stderr)
    sys.exit(66)

cfg = json.loads(CONFIG_PATH.read_text())
PROJECT_OWNER: str = cfg["project"]["owner"]
PROJECT_NUMBER: int = int(cfg["project"]["number"])
REPO_REMOTE: str = cfg["repo"]["remote"]
REPO_NWO: str = re.sub(r"^https?://github\.com/", "", REPO_REMOTE)
REPO_NWO = re.sub(r"\.git$", "", REPO_NWO)
RUNS_DIR = cfg.get("paths", {}).get("runs_dir", "docs/super-board/runs")
RUN_DATE = datetime.date.today().isoformat()
MANIFEST_PATH = Path(RUNS_DIR) / f"{RUN_DATE}-{CONFIG_SLUG}.md"

TICK_MIN = int(cfg.get("sentry", {}).get("tick_minutes", 2))
HEARTBEAT_MIN = int(cfg.get("sentry", {}).get("heartbeat_minutes", 15))
BASE_BRANCH: str = cfg.get("base_branch", "main")

STATE_DIR = Path(".claude/super-board")
STATE_FILE = STATE_DIR / "sentry-state.json"
STATE_DIR.mkdir(parents=True, exist_ok=True)


# ───────────────────────────── gh helpers ─────────────────────────────


def gh(*args: str) -> tuple[bool, str]:
    """Run gh and return (ok, stdout)."""
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, encoding="utf-8", check=False
        )
    except FileNotFoundError:
        return False, ""
    return proc.returncode == 0, proc.stdout


# ───────────────────────────── GitHub: project items ─────────────────────────────
# Same lightweight GraphQL query as super-board-status.py.

ITEMS_QUERY = """
query($owner:String!, $number:Int!) {
  user(login:$owner) {
    projectV2(number:$number) {
      items(first:100) {
        nodes {
          content { ... on Issue { number title labels(first:20){nodes{name}} } }
          fieldValues(first:8) {
            nodes { ... on ProjectV2ItemFieldSingleSelectValue {
              name field { ... on ProjectV2SingleSelectField { name } } } }
          }
        }
      }
    }
  }
}
"""

gh_ok, items_stdout = gh(
    "api", "graphql",
    "-f", f"query={ITEMS_QUERY}",
    "-F", f"owner={PROJECT_OWNER}",
    "-F", f"number={PROJECT_NUMBER}",
)
items_raw: list[dict[str, Any]] = []
if gh_ok:
    try:
        payload = json.loads(items_stdout)
        if payload.get("errors"):
            gh_ok = False
        else:
            items_raw = (
                payload.get("data", {})
                .get("user", {})
                .get("projectV2", {})
                .get("items", {})
                .get("nodes", [])
            ) or []
    except json.JSONDecodeError:
        gh_ok = False


# ───────────────────────────── GitHub: recent merged PRs ─────────────────────────────
# Last 10 merged PRs targeting our base branch.

merged_raw: list[dict[str, Any]] = []
if gh_ok:
    ok2, prs_stdout = gh(
        "pr", "list",
        "--repo", REPO_NWO,
        "--state", "merged",
        "--base", BASE_BRANCH,
        "--limit", "10",
        "--json", "number,title,url,mergedAt,body",
    )
    if ok2:
        try:
            merged_raw = json.loads(prs_stdout) or []
        except json.JSONDecodeError:
            # Treat as a soft failure — JSON corruption means the call
            # technically returned but we got nothing usable. Roll it into
            # the same failure-streak so an extended outage still alerts.
            gh_ok = False
    else:
        # `gh pr list` is the only other gh call this tick. Without folding
        # its failure into `gh_ok`, a persistent REST outage with a healthy
        # GraphQL endpoint would never trip the gh-down alert.
        gh_ok = False


# ───────────────────────────── manifest read ─────────────────────────────

manifest = ""
manifest_size = 0
if MANIFEST_PATH.is_file():
    manifest_bytes = MANIFEST_PATH.read_bytes()
    manifest = manifest_bytes.decode("utf-8", errors="replace")
    manifest_size = len(manifest_bytes)


# ───────────────────────────── state file: load or init ─────────────────────────────

state: dict[str, Any] = {}
if STATE_FILE.is_file():
    try:
        state = json.loads(STATE_FILE.read_text()) or {}
    except json.JSONDecodeError:
        state = {}

now = int(time.time())
now_iso = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
today = RUN_DATE


# ───────────────────────────── flatten items ─────────────────────────────


def field_status(node: dict[str, Any]) -> str:
    for fv in (node.get("fieldValues", {}).get("nodes") or []):
        if fv and fv.get("field", {}).get("name") == "Status":
            return fv.get("name") or "Backlog"
    return "Backlog"


items_by_n: dict[int, dict[str, Any]] = {}
for n in items_raw:
    c = n.get("content") or {}
    if not c.get("number"):
        continue
    items_by_n[c["number"]] = {
        "title": c.get("title") or "",
        "status": field_status(n),
        "labels": [
            l["name"]
            for l in (c.get("labels", {}).get("nodes") or [])
            if l.get("name")
        ],
    }

current_column_state = {str(k): v["status"] for k, v in items_by_n.items()}
last_column_state: dict[str, str] = state.get("last_column_state") or {}
seen_merged: set[int] = set(state.get("seen_merged_prs") or [])
last_offset = int(state.get("last_manifest_byte_offset", 0) or 0)
last_manifest_path = state.get("last_manifest_path", "")
last_heartbeat_at = state.get("last_heartbeat_at", "")
gh_fail_streak = int(state.get("gh_failure_streak", 0) or 0)


# ───────────────────────────── manifest delta ─────────────────────────────
# Reset offset on date rollover (filename changed).

if last_manifest_path and last_manifest_path != str(MANIFEST_PATH):
    last_offset = 0

manifest_bytes_b = manifest.encode("utf-8") if manifest else b""
new_tail = (
    manifest_bytes_b[last_offset:].decode("utf-8", errors="replace")
    if manifest_bytes_b
    else ""
)


# ───────────────────────────── event extraction ─────────────────────────────

TS_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\] (.*)$")
DISPATCH_RE = re.compile(r"dispatch lane=([a-z]+) issue=#(\d+) pid=(\d+).*attempt=(\d+)/3")
REAP_RE = re.compile(r"reaped stale lock.*on #(\d+) \(pid=(\d+)\)")
ZOMBIE_RE = re.compile(r"zombie [a-z]+ worker on #(\d+) \(pid=(\d+)\)(.*)$")
ALERT_RE = re.compile(r"block-rate alert: (.+)$")
START_RE = re.compile(r"super-board run started")
STOP_RE = re.compile(r"STOP 🛑 stopping dispatcher loop|exiting cleanly")


def hms_to_epoch(hms: str) -> int:
    try:
        return int(
            datetime.datetime.strptime(f"{today} {hms}", "%Y-%m-%d %H:%M:%S").timestamp()
        )
    except Exception:
        return now


events: list[tuple[int, str, dict[str, Any]]] = []

for line in new_tail.splitlines():
    m = TS_RE.match(line)
    if not m:
        continue
    h, mi, s, rest = m.groups()
    hms = f"{h}:{mi}:{s}"
    ep = hms_to_epoch(hms)

    if START_RE.search(rest):
        events.append((ep, "run-start", {"hms": hms}))
        continue
    if STOP_RE.search(rest):
        if not any(e[1] == "run-stop" for e in events):
            events.append((ep, "run-stop", {"hms": hms}))
        continue
    if dm := DISPATCH_RE.search(rest):
        lane, issue, _pid, attempt = dm.groups()
        events.append((ep, "dispatch", {
            "lane": lane, "issue": int(issue), "attempt": int(attempt), "hms": hms,
        }))
        continue
    if rm := REAP_RE.search(rest):
        issue, _pid = rm.groups()
        events.append((ep, "reap", {"issue": int(issue), "hms": hms}))
        continue
    if zm := ZOMBIE_RE.search(rest):
        issue, _pid, det = zm.groups()
        events.append((ep, "zombie", {
            "issue": int(issue), "detail": det.strip(" —"), "hms": hms,
        }))
        continue
    if am := ALERT_RE.search(rest):
        events.append((ep, "block-rate", {"detail": am.group(1), "hms": hms}))


# ───────────────────────────── column transitions ─────────────────────────────
# Detect cards that moved INTO Done, Blocked, or Skipped since last tick.
# Cards entering Building/QA/Review are usually surfaced via dispatch lines
# already; we don't double-announce here.

for n_str, status in current_column_state.items():
    n = int(n_str)
    prev = last_column_state.get(n_str)
    if prev == status:
        continue
    if status in ("Done", "Blocked", "Skipped") and prev != status:
        events.append((now, f"column-{status.lower()}", {"issue": n, "from": prev or "?"}))


# ───────────────────────────── merged PRs ─────────────────────────────


def issue_from_body(body: str | None) -> int | None:
    m = re.search(r"(?i)\b(?:closes|fixes|resolves)\s+#(\d+)", body or "")
    return int(m.group(1)) if m else None


new_merged: list[dict[str, Any]] = []
for pr in merged_raw:
    n = pr.get("number")
    if not n or n in seen_merged:
        continue
    new_merged.append(pr)
    seen_merged.add(n)

for pr in new_merged:
    events.append((now, "merged", {
        "pr_number": pr["number"],
        "pr_title": pr.get("title", ""),
        "pr_url": pr.get("url", ""),
        "issue": issue_from_body(pr.get("body", "")),
    }))

events.sort(key=lambda e: e[0])


# ───────────────────────────── gh failure tracking ─────────────────────────────

if not gh_ok:
    gh_fail_streak += 1
    if gh_fail_streak == 3:
        events.append((now, "gh-down", {"hms": "now"}))
else:
    gh_fail_streak = 0


# ───────────────────────────── rendering helpers ─────────────────────────────


def visual_width(s: str) -> int:
    w = 0
    for c in s:
        if unicodedata.category(c) == "Mn" or ord(c) == 0xFE0F:
            continue
        if unicodedata.east_asian_width(c) in ("W", "F"):
            w += 2
        elif ord(c) >= 0x2600:
            w += 2
        else:
            w += 1
    return w


ALERT_WIDTH = 62  # narrower than 80 — alerts are punchy, not boxy


def hh_mm(ep: int) -> str:
    return datetime.datetime.fromtimestamp(ep).strftime("%H:%M")


def alert_header(glyph: str, verb: str, ts: str) -> str:
    left = f"─── {glyph} {verb} "
    right = f" {ts}"
    fill = ALERT_WIDTH - visual_width(left) - visual_width(right)
    return left + ("─" * max(fill, 1)) + right


def alert_footer() -> str:
    return "─" * ALERT_WIDTH


LANE_GLYPH = {"build": "🔨", "qa": "🔍", "review": "✏️"}
LANE_NAME = {"build": "Builder", "qa": "Tester", "review": "Reviewer"}
LANE_DEST = {"build": "Building", "qa": "QA", "review": "Review"}


def render_event(kind: str, p: dict[str, Any]) -> tuple[list[str], str | None, str | None]:
    """Return (terminal_block, telegram_msg, tg_key)."""
    if kind == "dispatch":
        glyph = LANE_GLYPH[p["lane"]]
        name = LANE_NAME[p["lane"]]
        dest = LANE_DEST[p["lane"]]
        title = (items_by_n.get(p["issue"], {}).get("title") or "")[:50]
        ts = p["hms"][:5]
        lines = [
            alert_header(glyph, "DISPATCH", ts),
            f"   #{p['issue']}  {title}",
            f"   → {dest} as {name}, attempt {p['attempt']}/3",
            alert_footer(),
        ]
        return lines, f"{glyph} #{p['issue']} dispatched → {dest} (attempt {p['attempt']}/3)", "dispatch"

    if kind == "reap":
        title = (items_by_n.get(p["issue"], {}).get("title") or "")[:50]
        ts = p["hms"][:5]
        lines = [
            alert_header("♻", "REAP", ts),
            f"   #{p['issue']}  {title}",
            "   stale lock + assignee swept",
            alert_footer(),
        ]
        return lines, f"♻ #{p['issue']} reaped — stale lock + assignee swept", "reap"

    if kind == "zombie":
        title = (items_by_n.get(p["issue"], {}).get("title") or "")[:50]
        ts = p["hms"][:5]
        lines = [
            alert_header("💀", "ZOMBIE", ts),
            f"   #{p['issue']}  {title}",
            f"   {p['detail']}",
            alert_footer(),
        ]
        return lines, f"💀 #{p['issue']} zombie worker — {p['detail']}", "zombie"

    if kind == "merged":
        n = p["issue"]
        title = (items_by_n.get(n, {}).get("title") or p["pr_title"] or "")[:50]
        ts = hh_mm(now)
        lines = [
            alert_header("✅", "MERGED", ts),
            f"   #{n or '?'}  {title}",
            f"   PR      {p['pr_url']}",
        ]
        target_url = (cfg.get("target") or {}).get("url")
        if target_url:
            lines.append(f"   preview {target_url}")
        lines.append(alert_footer())
        return lines, f"✅ #{n or '?'} merged — {title}\n{p['pr_url']}", "merged"

    if kind == "column-blocked":
        title = (items_by_n.get(p["issue"], {}).get("title") or "")[:50]
        ts = hh_mm(now)
        lines = [
            alert_header("⛔", "BLOCKED", ts),
            f"   #{p['issue']}  {title}",
            f"   was: {p['from']}",
            alert_footer(),
        ]
        return lines, f"⛔ #{p['issue']} blocked — {title}", "blocked"

    if kind == "column-skipped":
        title = (items_by_n.get(p["issue"], {}).get("title") or "")[:50]
        ts = hh_mm(now)
        lines = [
            alert_header("⏭", "SKIPPED", ts),
            f"   #{p['issue']}  {title}",
            f"   was: {p['from']}",
            alert_footer(),
        ]
        return lines, f"⏭ #{p['issue']} skipped — {title}", "skipped"

    if kind == "column-done":
        # We usually announce merges separately; only fire this if the card
        # landed in Done without a matching PR alert this tick.
        title = (items_by_n.get(p["issue"], {}).get("title") or "")[:50]
        ts = hh_mm(now)
        lines = [
            alert_header("✅", "DONE", ts),
            f"   #{p['issue']}  {title}",
            f"   moved from {p['from']}",
            alert_footer(),
        ]
        return lines, f"✅ #{p['issue']} done — {title}", "done"

    if kind == "block-rate":
        ts = p["hms"][:5]
        lines = [
            alert_header("⚠", "BLOCK-RATE ALERT", ts),
            f"   {p['detail']}",
            alert_footer(),
        ]
        return lines, f"⚠ block-rate alert — {p['detail']}", "block-rate"

    if kind == "run-start":
        ts = p["hms"][:5]
        lines = [
            alert_header("▶", "RUN STARTED", ts),
            "   dispatcher loop is live",
            alert_footer(),
        ]
        return lines, "▶ super-board run started", "run-start-stop"

    if kind == "run-stop":
        ts = p["hms"][:5]
        lines = [
            alert_header("⏹", "RUN STOPPED", ts),
            "   dispatcher loop exited",
            alert_footer(),
        ]
        return lines, "⏹ super-board run stopped", "run-start-stop"

    if kind == "gh-down":
        ts = hh_mm(now)
        lines = [
            alert_header("⚠", "SENTRY: GH UNAVAILABLE", ts),
            "   3 consecutive gh calls failed — alerts may be incomplete",
            alert_footer(),
        ]
        return lines, None, None

    return [], None, None


# Suppress column-done events whose issue was already covered by a merged PR.
merged_issues_this_tick = {p["issue"] for _, k, p in events if k == "merged" and p.get("issue")}
filtered_events = [
    (ep, k, p)
    for (ep, k, p) in events
    if not (k == "column-done" and p.get("issue") in merged_issues_this_tick)
]


# ───────────────────────────── heartbeat boundary ─────────────────────────────


def parse_iso(s: str) -> int:
    # NOW_EPOCH is a real Unix epoch (UTC). Persisted ISO strings end in `Z`,
    # so attach UTC explicitly before .timestamp() — otherwise the naive
    # datetime gets interpreted as local time.
    try:
        dt = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
        return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
    except Exception:
        return 0


last_hb_epoch = parse_iso(last_heartbeat_at) if last_heartbeat_at else 0
hb_due = (now - last_hb_epoch) >= (HEARTBEAT_MIN * 60)

tg_alerts = set((cfg.get("sentry", {}) or {}).get("telegram_alerts", []) or [])


# ───────────────────────────── emit ─────────────────────────────


def persist_state(new_state: dict[str, Any]) -> None:
    # Atomic write: a Ctrl-C between `write_text` start and finish would
    # leave the JSON half-flushed and unparseable on next start. tempfile +
    # `os.replace` keeps the visible file either fully old or fully new.
    # Same-directory rename is atomic on POSIX and Windows.
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(new_state, indent=2))
    os.replace(tmp, STATE_FILE)


if first_run:
    # The orchestrator handles the "Sentry armed" banner + the baseline
    # status snapshot itself (it has the AskUserQuestion context for
    # Telegram setup). We just initialize the state file and exit quietly
    # so the next tick starts from this moment forward.
    persist_state({
        "last_manifest_path": str(MANIFEST_PATH),
        "last_manifest_byte_offset": manifest_size,
        "last_column_state": current_column_state,
        "last_heartbeat_at": now_iso,
        "last_tick_at": now_iso,
        "seen_merged_prs": sorted(seen_merged)[-SEEN_MERGED_RETAIN:],
        "gh_failure_streak": gh_fail_streak,
    })
    print(f"__NEXT_TICK_SECONDS__:{TICK_MIN * 60}")
    sys.exit(0)


emitted_any = False
for ep, kind, p in filtered_events:
    block, tg_msg, tg_key = render_event(kind, p)
    if not block:
        continue
    if emitted_any:
        print()  # blank line between alerts
    print("\n".join(block))
    emitted_any = True
    if tg_msg and tg_key and tg_key in tg_alerts:
        tg_safe = tg_msg.replace("\n", "\\n")
        print(f"TELEGRAM:{tg_key}:{tg_safe}")


# ───────────────────────────── heartbeat ─────────────────────────────

new_heartbeat_at = last_heartbeat_at
if hb_due:
    if emitted_any:
        print()
    # Reuse the existing status script for the locked snapshot. Look for it
    # next to ourselves first (works both for `.claude/bin/` install and the
    # dev checkout under `scripts/`), then fall back to `python3` on PATH.
    status_script = Path(__file__).with_name("super-board-status.py")
    try:
        out = subprocess.run(
            [sys.executable, str(status_script), CONFIG_SLUG],
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
        )
        if out.returncode == 0:
            print(out.stdout.rstrip())
            new_heartbeat_at = now_iso
        else:
            print(f"⚠ heartbeat: super-board-status.py exited {out.returncode}")
            if out.stderr.strip():
                print(f"   {out.stderr.strip()}")
    except Exception as e:
        print(f"⚠ heartbeat: {e}")


# ───────────────────────────── persist state ─────────────────────────────

persist_state({
    "last_manifest_path": str(MANIFEST_PATH),
    "last_manifest_byte_offset": manifest_size,
    "last_column_state": current_column_state,
    "last_heartbeat_at": new_heartbeat_at,
    "last_tick_at": now_iso,
    "seen_merged_prs": sorted(seen_merged)[-SEEN_MERGED_RETAIN:],
    "gh_failure_streak": gh_fail_streak,
})

print(f"__NEXT_TICK_SECONDS__:{TICK_MIN * 60}")
