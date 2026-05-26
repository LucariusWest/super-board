#!/usr/bin/env bash
# super-board-sentry.sh — one tick of the super-board sentry watcher.
#
# Called by the orchestrator (interactive Claude session) once every
# `tick_minutes` (default 2). Reads the run manifest delta, fetches column
# counts, diffs against the last-seen state file, and prints zero or more
# alert blocks plus an optional 15-minute heartbeat snapshot.
#
# Read-only contract: same forbidden set as `super-board status`. No
# mutations, no labels, no assignee writes. State file write is local FS.
#
# Output protocol — every line goes to stdout. The orchestrator prints
# everything verbatim EXCEPT lines beginning with one of these prefixes:
#
#   TELEGRAM:<event_type>:<message>    queue a Telegram fan-out (filtered
#                                      by config.sentry.telegram_alerts)
#   __NEXT_TICK_SECONDS__:<N>          tell the orchestrator how long to
#                                      sleep before the next tick
#
# Usage:
#   scripts/super-board-sentry.sh [--first-run] [<config-slug>]
#
# Exit codes:
#   0  ok (output may or may not contain alerts)
#   64 missing arg + no active marker + no single config
#   66 config not found
#   67 gh / network failure (3rd consecutive)

set -euo pipefail

# ───────────────────────────── args + paths ─────────────────────────────
FIRST_RUN=false
if [ "${1:-}" = "--first-run" ]; then
  FIRST_RUN=true
  shift
fi

CONFIG_SLUG="${1:-}"
if [ -z "$CONFIG_SLUG" ]; then
  if [ -f .claude/super-board/active ]; then
    CONFIG_SLUG=$(cat .claude/super-board/active)
  else
    _cfgs=( .claude/super-board/configs/*.json )
    if [ -f "${_cfgs[0]}" ] && [ "${#_cfgs[@]}" -eq 1 ]; then
      CONFIG_SLUG=$(basename "${_cfgs[0]}" .json)
    else
      echo "usage: $0 [--first-run] <config-slug>" >&2
      exit 64
    fi
  fi
fi

CONFIG_PATH=".claude/super-board/configs/${CONFIG_SLUG}.json"
if [ ! -f "$CONFIG_PATH" ]; then
  echo "config not found: $CONFIG_PATH" >&2
  exit 66
fi

STATE_DIR=".claude/super-board"
STATE_FILE="${STATE_DIR}/sentry-state.json"
mkdir -p "$STATE_DIR"

PROJECT_OWNER=$(jq -r '.project.owner' "$CONFIG_PATH")
PROJECT_NUMBER=$(jq -r '.project.number' "$CONFIG_PATH")
REPO_REMOTE=$(jq -r '.repo.remote' "$CONFIG_PATH")
# Parse "https://github.com/Owner/Repo.git" → "Owner/Repo"
REPO_NWO=$(echo "$REPO_REMOTE" \
  | sed -E 's#https?://github\.com/##; s#\.git$##')
RUNS_DIR=$(jq -r '.paths.runs_dir // "docs/super-board/runs"' "$CONFIG_PATH")
RUN_DATE=$(date +%Y-%m-%d)
MANIFEST="${RUNS_DIR}/${RUN_DATE}-${CONFIG_SLUG}.md"

TICK_MIN=$(jq -r '.sentry.tick_minutes // 2' "$CONFIG_PATH")
HEARTBEAT_MIN=$(jq -r '.sentry.heartbeat_minutes // 15' "$CONFIG_PATH")

# ───────────────────────────── GitHub: project items ─────────────────────────────
# Same lightweight GraphQL query as super-board-status. We cache the result
# alongside the state file so the heartbeat path can reuse it without a
# second round-trip.
ITEMS_JSON=""
GH_OK=true
ITEMS_JSON=$(gh api graphql -f query='
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
}' -F owner="$PROJECT_OWNER" -F number="$PROJECT_NUMBER" 2>/dev/null) || GH_OK=false

if [ "$GH_OK" = "true" ]; then
  if echo "$ITEMS_JSON" | jq -e '.errors' >/dev/null 2>&1; then
    GH_OK=false
  fi
fi

# ───────────────────────────── GitHub: recent merged PRs ─────────────────────────────
# Last 10 merged PRs targeting our base branch. We filter against
# seen_merged_prs in the renderer so we only alert once per PR.
BASE_BRANCH=$(jq -r '.base_branch' "$CONFIG_PATH")
MERGED_PRS_JSON="[]"
if [ "$GH_OK" = "true" ]; then
  MERGED_PRS_JSON=$(gh pr list \
    --repo "$REPO_NWO" \
    --state merged \
    --base "$BASE_BRANCH" \
    --limit 10 \
    --json number,title,url,mergedAt,body \
    2>/dev/null) || MERGED_PRS_JSON="[]"
fi

# ───────────────────────────── manifest read ─────────────────────────────
MANIFEST_BODY=""
MANIFEST_SIZE=0
if [ -f "$MANIFEST" ]; then
  MANIFEST_BODY=$(cat "$MANIFEST")
  # `wc -c` includes the trailing newline; this is what we'll store as
  # the byte offset for next tick.
  MANIFEST_SIZE=$(wc -c < "$MANIFEST" | tr -d ' ')
fi

# ───────────────────────────── state file: load or init ─────────────────────────────
STATE_JSON="{}"
if [ -f "$STATE_FILE" ]; then
  STATE_JSON=$(cat "$STATE_FILE")
fi

# ───────────────────────────── pipe to python renderer ─────────────────────────────
NOW_EPOCH=$(date +%s)
NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

PAYLOAD=$(mktemp -t super-board-sentry.XXXXXX.json)
trap 'rm -f "$PAYLOAD"' EXIT

jq -n \
  --argjson items "${ITEMS_JSON:-{\}}" \
  --argjson merged "$MERGED_PRS_JSON" \
  --argjson state "$STATE_JSON" \
  --arg manifest "$MANIFEST_BODY" \
  --arg manifest_path "$MANIFEST" \
  --argjson manifest_size "$MANIFEST_SIZE" \
  --arg config_path "$CONFIG_PATH" \
  --arg slug "$CONFIG_SLUG" \
  --arg today "$RUN_DATE" \
  --argjson now "$NOW_EPOCH" \
  --arg now_iso "$NOW_ISO" \
  --argjson tick_min "$TICK_MIN" \
  --argjson heartbeat_min "$HEARTBEAT_MIN" \
  --arg first_run "$FIRST_RUN" \
  --arg gh_ok "$GH_OK" \
  --arg repo_nwo "$REPO_NWO" \
  '{
    items: ($items.data.user.projectV2.items.nodes // []),
    merged_prs: $merged,
    state: $state,
    manifest: $manifest,
    manifest_path: $manifest_path,
    manifest_size: $manifest_size,
    config_path: $config_path,
    slug: $slug,
    today: $today,
    now: $now,
    now_iso: $now_iso,
    tick_min: $tick_min,
    heartbeat_min: $heartbeat_min,
    first_run: ($first_run == "true"),
    gh_ok: ($gh_ok == "true"),
    repo_nwo: $repo_nwo
  }' > "$PAYLOAD"

python3 - "$PAYLOAD" "$STATE_FILE" <<'PYEOF'
import json, sys, re, os, unicodedata, datetime, subprocess

with open(sys.argv[1]) as fp:
    raw = json.load(fp)
STATE_FILE = sys.argv[2]

state         = raw["state"] or {}
items_raw     = raw["items"] or []
merged_raw    = raw["merged_prs"] or []
manifest      = raw["manifest"] or ""
manifest_path = raw["manifest_path"]
manifest_size = raw["manifest_size"]
slug          = raw["slug"]
today         = raw["today"]
now           = int(raw["now"])
now_iso       = raw["now_iso"]
tick_min      = int(raw["tick_min"])
heartbeat_min = int(raw["heartbeat_min"])
first_run     = bool(raw["first_run"])
gh_ok         = bool(raw["gh_ok"])
repo_nwo      = raw["repo_nwo"]

# ───── flatten items into {number: {title, status, labels}} ─────
def field_status(node):
    for fv in (node.get("fieldValues", {}).get("nodes") or []):
        if fv and fv.get("field", {}).get("name") == "Status":
            return fv.get("name") or "Backlog"
    return "Backlog"

items_by_n = {}
for n in items_raw:
    c = n.get("content") or {}
    if not c.get("number"):
        continue
    items_by_n[c["number"]] = {
        "title": c.get("title") or "",
        "status": field_status(n),
        "labels": [l["name"] for l in (c.get("labels", {}).get("nodes") or []) if l.get("name")],
    }

current_column_state = {str(k): v["status"] for k, v in items_by_n.items()}
last_column_state    = state.get("last_column_state", {}) or {}
seen_merged          = set(state.get("seen_merged_prs", []) or [])
last_offset          = int(state.get("last_manifest_byte_offset", 0) or 0)
last_manifest_path   = state.get("last_manifest_path", "")
last_heartbeat_at    = state.get("last_heartbeat_at", "")
gh_fail_streak       = int(state.get("gh_failure_streak", 0) or 0)

# ───── manifest delta ─────
# Reset offset on date rollover (filename changed).
if last_manifest_path and last_manifest_path != manifest_path:
    last_offset = 0

# Read only new bytes from the manifest. We received the full file in
# `manifest`; slice the new tail.
manifest_bytes = manifest.encode("utf-8") if manifest else b""
new_tail = manifest_bytes[last_offset:].decode("utf-8", errors="replace") if manifest_bytes else ""

# ───── event extraction ─────
TS_RE       = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\] (.*)$")
DISPATCH_RE = re.compile(r"dispatch lane=([a-z]+) issue=#(\d+) pid=(\d+).*attempt=(\d+)/3")
REAP_RE     = re.compile(r"reaped stale lock.*on #(\d+) \(pid=(\d+)\)")
ZOMBIE_RE   = re.compile(r"zombie [a-z]+ worker on #(\d+) \(pid=(\d+)\)(.*)$")
ALERT_RE    = re.compile(r"block-rate alert: (.+)$")
START_RE    = re.compile(r"super-board run started")
STOP_RE     = re.compile(r"STOP 🛑 stopping dispatcher loop|exiting cleanly")

events = []  # list of (epoch, kind, payload_dict)

def hms_to_epoch(hms):
    try:
        return int(datetime.datetime.strptime(f"{today} {hms}", "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        return now

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
        # Only emit once per stop sequence — collapse all STOP lines to one.
        if not any(e[1] == "run-stop" for e in events):
            events.append((ep, "run-stop", {"hms": hms}))
        continue
    dm = DISPATCH_RE.search(rest)
    if dm:
        lane, issue, pid, attempt = dm.groups()
        events.append((ep, "dispatch", {
            "lane": lane, "issue": int(issue), "attempt": int(attempt), "hms": hms,
        }))
        continue
    rm = REAP_RE.search(rest)
    if rm:
        issue, pid = rm.groups()
        events.append((ep, "reap", {"issue": int(issue), "hms": hms}))
        continue
    zm = ZOMBIE_RE.search(rest)
    if zm:
        issue, pid, det = zm.groups()
        events.append((ep, "zombie", {"issue": int(issue), "detail": det.strip(" —"), "hms": hms}))
        continue
    am = ALERT_RE.search(rest)
    if am:
        events.append((ep, "block-rate", {"detail": am.group(1), "hms": hms}))

# ───── column transitions (Projects v2 diff) ─────
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

# ───── merged PRs ─────
def issue_from_body(body):
    # Match "Closes #N", "Fixes #N", "Resolves #N" (case-insensitive).
    m = re.search(r"(?i)\b(?:closes|fixes|resolves)\s+#(\d+)", body or "")
    return int(m.group(1)) if m else None

new_merged = []
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

# Sort events by time so terminal output reads chronologically.
events.sort(key=lambda e: e[0])

# ───── gh failure tracking ─────
if not gh_ok:
    gh_fail_streak += 1
    if gh_fail_streak == 3:
        events.append((now, "gh-down", {"hms": "now"}))
else:
    gh_fail_streak = 0

# ───── rendering helpers ─────
def visual_width(s):
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

def pad_to(s, width):
    diff = width - visual_width(s)
    return s + (" " * diff) if diff > 0 else s

def hh_mm(ep):
    return datetime.datetime.fromtimestamp(ep).strftime("%H:%M")

def alert_header(glyph, verb, ts):
    # `─── <glyph> VERB ──── <HH:MM>` padded to ALERT_WIDTH chars.
    left = f"─── {glyph} {verb} "
    right = f" {ts}"
    fill = ALERT_WIDTH - visual_width(left) - visual_width(right)
    return left + ("─" * max(fill, 1)) + right

def alert_footer():
    return "─" * ALERT_WIDTH

LANE_GLYPH = {"build": "🔨", "qa": "🔍", "review": "✏️"}
LANE_NAME  = {"build": "Builder", "qa": "Tester", "review": "Reviewer"}
LANE_DEST  = {"build": "Building", "qa": "QA", "review": "Review"}

def render_event(kind, p):
    """Return (terminal_block: list[str], telegram_msg: str|None, tg_key: str)."""
    if kind == "dispatch":
        glyph = LANE_GLYPH[p["lane"]]
        name  = LANE_NAME[p["lane"]]
        dest  = LANE_DEST[p["lane"]]
        title = (items_by_n.get(p["issue"], {}).get("title") or "")[:50]
        ts    = p["hms"][:5]
        lines = [
            alert_header(glyph, "DISPATCH", ts),
            f"   #{p['issue']}  {title}",
            f"   → {dest} as {name}, attempt {p['attempt']}/3",
            alert_footer(),
        ]
        tg = f"{glyph} #{p['issue']} dispatched → {dest} (attempt {p['attempt']}/3)"
        return lines, tg, "dispatch"

    if kind == "reap":
        title = (items_by_n.get(p["issue"], {}).get("title") or "")[:50]
        ts    = p["hms"][:5]
        lines = [
            alert_header("♻", "REAP", ts),
            f"   #{p['issue']}  {title}",
            "   stale lock + assignee swept",
            alert_footer(),
        ]
        tg = f"♻ #{p['issue']} reaped — stale lock + assignee swept"
        return lines, tg, "reap"

    if kind == "zombie":
        title = (items_by_n.get(p["issue"], {}).get("title") or "")[:50]
        ts    = p["hms"][:5]
        lines = [
            alert_header("💀", "ZOMBIE", ts),
            f"   #{p['issue']}  {title}",
            f"   {p['detail']}",
            alert_footer(),
        ]
        return lines, f"💀 #{p['issue']} zombie worker — {p['detail']}", "zombie"

    if kind == "merged":
        n     = p["issue"]
        title = (items_by_n.get(n, {}).get("title") or p["pr_title"] or "")[:50]
        ts    = hh_mm(now)
        lines = [
            alert_header("✅", "MERGED", ts),
            f"   #{n or '?'}  {title}",
            f"   PR      {p['pr_url']}",
        ]
        # Add preview link if the config carries one.
        target_url = None
        try:
            with open(raw["config_path"]) as f:
                target_url = json.load(f).get("target", {}).get("url")
        except Exception:
            pass
        if target_url:
            lines.append(f"   preview {target_url}")
        lines.append(alert_footer())
        tg = f"✅ #{n or '?'} merged — {title}\n{p['pr_url']}"
        return lines, tg, "merged"

    if kind == "column-blocked":
        title = (items_by_n.get(p["issue"], {}).get("title") or "")[:50]
        ts    = hh_mm(now)
        lines = [
            alert_header("⛔", "BLOCKED", ts),
            f"   #{p['issue']}  {title}",
            f"   was: {p['from']}",
            alert_footer(),
        ]
        return lines, f"⛔ #{p['issue']} blocked — {title}", "blocked"

    if kind == "column-skipped":
        title = (items_by_n.get(p["issue"], {}).get("title") or "")[:50]
        ts    = hh_mm(now)
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
        ts    = hh_mm(now)
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
    (ep, k, p) for (ep, k, p) in events
    if not (k == "column-done" and p.get("issue") in merged_issues_this_tick)
]

# ───── decide if first-run / heartbeat boundary ─────
def parse_iso(s):
    # NOW_EPOCH is a real Unix epoch (UTC). The ISO strings we persist end
    # in `Z`, so attach UTC explicitly before .timestamp() — otherwise the
    # naive datetime gets interpreted as local time and the heartbeat
    # check is wrong by the user's UTC offset.
    try:
        dt = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
        return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
    except Exception:
        return 0

last_hb_epoch = parse_iso(last_heartbeat_at) if last_heartbeat_at else 0
hb_due = (now - last_hb_epoch) >= (heartbeat_min * 60)

# Load telegram preferences for fan-out filtering.
try:
    with open(raw["config_path"]) as f:
        cfg = json.load(f)
except Exception:
    cfg = {}
tg_alerts = set((cfg.get("sentry", {}) or {}).get("telegram_alerts", []) or [])

# ───── emit ─────
if first_run:
    # The orchestrator handles the "Sentry armed" banner + the baseline
    # status snapshot itself (it has the AskUserQuestion context for
    # Telegram setup). We just initialize the state file and exit quietly
    # so the next tick starts from this moment forward.
    new_state = {
        "last_manifest_path": manifest_path,
        "last_manifest_byte_offset": manifest_size,
        "last_column_state": current_column_state,
        "last_heartbeat_at": now_iso,
        "last_tick_at": now_iso,
        "seen_merged_prs": sorted(seen_merged),
        "gh_failure_streak": gh_fail_streak,
    }
    with open(STATE_FILE, "w") as f:
        json.dump(new_state, f, indent=2)
    print(f"__NEXT_TICK_SECONDS__:{tick_min * 60}")
    sys.exit(0)

# Emit alerts (chronological).
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
        # One-line escape — the orchestrator splits on the first colon
        # pair to extract event_type and message body.
        tg_safe = tg_msg.replace("\n", "\\n")
        print(f"TELEGRAM:{tg_key}:{tg_safe}")

# Emit heartbeat if due.
new_heartbeat_at = last_heartbeat_at
if hb_due:
    if emitted_any:
        print()
    try:
        # Reuse the existing status script for the locked snapshot.
        out = subprocess.run(
            ["scripts/super-board-status.sh", slug],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            print(out.stdout.rstrip())
            new_heartbeat_at = now_iso
        else:
            print(f"⚠ heartbeat: super-board-status.sh exited {out.returncode}")
    except Exception as e:
        print(f"⚠ heartbeat: {e}")

# ───── persist state ─────
new_state = {
    "last_manifest_path": manifest_path,
    "last_manifest_byte_offset": manifest_size,
    "last_column_state": current_column_state,
    "last_heartbeat_at": new_heartbeat_at,
    "last_tick_at": now_iso,
    "seen_merged_prs": sorted(seen_merged),
    "gh_failure_streak": gh_fail_streak,
}
with open(STATE_FILE, "w") as f:
    json.dump(new_state, f, indent=2)

# Always tell the orchestrator when to wake up next.
print(f"__NEXT_TICK_SECONDS__:{tick_min * 60}")
PYEOF
