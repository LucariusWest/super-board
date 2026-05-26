#!/usr/bin/env bash
# super-board-status.sh — read-only live snapshot of the super-board pipeline.
#
# Replaces the model-rendered status path in
# `.claude/skills/super-board/references/status.md`. Same output, much faster
# because rendering moves from token-by-token generation into a shell wrapper
# + a single Python renderer (no Anthropic round-trip on the box-drawing).
#
# What it does:
#   1. Resolve config slug: arg | `.claude/super-board/active` | sole config.
#   2. ONE GraphQL call for project items (number, title, labels, Status).
#   3. ONE `gh issue list` for in-flight issues (assignees + labels).
#   4. Pipe all of that + today's manifest to a Python renderer that prints
#      the 80-col snapshot matching the locked template in
#      references/status.md.
#
# What it does NOT do:
#   - Any GitHub mutations (read-only verb — same forbidden set as the skill).
#   - Touch the manifest, locks, or worktrees.
#   - Wait for or poll workers.
#
# Usage:
#   scripts/super-board-status.sh [<config-slug>]
#
# Exit codes:
#   0  ok
#   64 missing arg + no active marker + no single config
#   66 config not found
#   67 gh / network failure

set -euo pipefail

# ───────────────────────────── args + paths ─────────────────────────────
CONFIG_SLUG="${1:-}"
if [ -z "$CONFIG_SLUG" ]; then
  if [ -f .claude/super-board/active ]; then
    CONFIG_SLUG=$(cat .claude/super-board/active)
  else
    # Sole-config fallback: a single-project repo doesn't need to name it.
    _cfgs=( .claude/super-board/configs/*.json )
    if [ -f "${_cfgs[0]}" ] && [ "${#_cfgs[@]}" -eq 1 ]; then
      CONFIG_SLUG=$(basename "${_cfgs[0]}" .json)
    else
      echo "usage: $0 <config-slug>  (or set .claude/super-board/active)" >&2
      exit 64
    fi
  fi
fi

CONFIG_PATH=".claude/super-board/configs/${CONFIG_SLUG}.json"
if [ ! -f "$CONFIG_PATH" ]; then
  echo "config not found: $CONFIG_PATH" >&2
  exit 66
fi

PROJECT_OWNER=$(jq -r '.project.owner' "$CONFIG_PATH")
PROJECT_NUMBER=$(jq -r '.project.number' "$CONFIG_PATH")
RUNS_DIR=$(jq -r '.paths.runs_dir // "docs/super-board/runs"' "$CONFIG_PATH")
RUN_DATE=$(date +%Y-%m-%d)
MANIFEST="${RUNS_DIR}/${RUN_DATE}-${CONFIG_SLUG}.md"

# ───────────────────────────── GitHub: project items ─────────────────────────────
# Targeted GraphQL — number, title, labels, Status only. ~3 KB vs. 100+ KB for
# `gh project item-list --format json` (which slurps every issue body).
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
}' -F owner="$PROJECT_OWNER" -F number="$PROJECT_NUMBER" 2>/dev/null) || {
  echo "graphql call failed (auth? network?)" >&2; exit 67;
}
# Sanity: bail if the query came back with errors instead of data.
if echo "$ITEMS_JSON" | jq -e '.errors' >/dev/null 2>&1; then
  echo "graphql returned errors:" >&2
  echo "$ITEMS_JSON" | jq -r '.errors[]?.message' >&2
  exit 67
fi

# ───────────────────────────── reason-tag fetch (Blocked + Skipped only) ─────────
# For each Blocked/Skipped issue, grab its latest comment so the renderer can
# pick a reason emoji from the locked vocabulary. Skip silently on error so a
# stale token doesn't break the rest of the snapshot.
REASONS_JSON="{}"
for n in $(echo "$ITEMS_JSON" | jq -r '
  .data.user.projectV2.items.nodes[]?
  | select(.content.number != null)
  | . as $i
  | ($i.fieldValues.nodes // []) as $fv
  | ($fv | map(select(.field.name? == "Status")) | .[0].name // "") as $s
  | select($s == "Blocked" or $s == "Skipped")
  | .content.number
'); do
  body=$(gh issue view "$n" --json comments 2>/dev/null \
         | jq -r '(.comments // []) | sort_by(.createdAt) | (last // {}).body // ""' 2>/dev/null \
         | head -c 4000 || true)
  REASONS_JSON=$(jq --argjson n "$n" --arg b "$body" '. + {($n|tostring): $b}' <<<"$REASONS_JSON")
done

# ───────────────────────────── render via Python ─────────────────────────────
# Pass everything the renderer needs as one JSON blob on stdin.
MANIFEST_BODY=""
[ -f "$MANIFEST" ] && MANIFEST_BODY=$(cat "$MANIFEST")

# `now_epoch` is captured here so the script and renderer agree on "now".
NOW_EPOCH=$(date +%s)
TODAY="$RUN_DATE"

# Write the renderer's input payload to a tempfile (heredocs and stdin can't
# both feed python at once).
PAYLOAD=$(mktemp -t super-board-status.XXXXXX.json)
trap 'rm -f "$PAYLOAD"' EXIT
jq -n \
  --argjson items "$ITEMS_JSON" \
  --argjson reasons "$REASONS_JSON" \
  --arg manifest "$MANIFEST_BODY" \
  --arg config "$CONFIG_PATH" \
  --arg slug "$CONFIG_SLUG" \
  --arg today "$TODAY" \
  --argjson now "$NOW_EPOCH" \
  '{
    items: $items.data.user.projectV2.items.nodes,
    reasons: $reasons,
    manifest: $manifest,
    config_path: $config,
    slug: $slug,
    today: $today,
    now: $now
  }' > "$PAYLOAD"

python3 - "$PAYLOAD" <<'PYEOF'
import json, sys, re, os, unicodedata, datetime, time

with open(sys.argv[1]) as _fp:
    raw = json.load(_fp)
items_raw = raw["items"]
reasons = raw["reasons"] or {}
manifest = raw["manifest"]
slug = raw["slug"]
today = raw["today"]
now = int(raw["now"])

with open(raw["config_path"]) as f:
    cfg = json.load(f)

# ───── flatten items ─────
def field_status(node):
    for fv in (node.get("fieldValues", {}).get("nodes") or []):
        if fv and fv.get("field", {}).get("name") == "Status":
            return fv.get("name") or "Backlog"
    return "Backlog"

items = []
for n in items_raw or []:
    c = n.get("content") or {}
    if not c.get("number"):
        continue
    items.append({
        "number": c["number"],
        "title": c.get("title") or "",
        "labels": [l["name"] for l in (c.get("labels", {}).get("nodes") or []) if l.get("name")],
        "status": field_status(n),
    })

by_status = {s: sorted([i for i in items if i["status"] == s],
                       key=lambda x: -x["number"])
             for s in ("Ready", "Building", "QA", "Review", "Done", "Blocked", "Skipped")}

# ───── manifest parse ─────
TS_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\] (.*)$")
DISPATCH_RE = re.compile(r"dispatch lane=([a-z]+) issue=#(\d+) pid=(\d+).*attempt=(\d+)/3")
REAP_RE     = re.compile(r"reaped stale lock.*on #(\d+) \(pid=(\d+)\)")
ZOMBIE_RE   = re.compile(r"zombie [a-z]+ worker on #(\d+) \(pid=(\d+)\)(.*)$")
ALERT_RE    = re.compile(r"block-rate alert: (.+)$")

def hms_to_epoch(hms):
    try:
        dt = datetime.datetime.strptime(f"{today} {hms}", "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp())
    except Exception:
        return 0

inflight = {}            # lane -> {pid, issue, attempt, ts_hms}
recents = []             # list of dicts: {epoch, verb, glyph, issue, target, detail}
last_tick = None
start_hms = None
exited = False
reaped_count = 0

for line in (manifest or "").splitlines():
    m = TS_RE.match(line)
    if not m:
        continue
    h, mi, s, rest = m.groups()
    hms = f"{h}:{mi}:{s}"
    ep = hms_to_epoch(hms)

    if "super-board run started" in rest:
        start_hms = hms; exited = False; continue
    if "exiting cleanly" in rest:
        exited = True; continue
    if rest.startswith("tick "):
        last_tick = hms; continue
    if rest.startswith("reaped worktree"):
        reaped_count += 1; continue

    dm = DISPATCH_RE.search(rest)
    if dm:
        lane, issue, pid, attempt = dm.groups()
        inflight[lane] = {"pid": pid, "issue": issue, "attempt": attempt, "ts": hms}
        glyph = {"build":"🔨","qa":"🔍","review":"✏️"}[lane]
        target = {"build":"Building","qa":"QA","review":"Review"}[lane]
        recents.append({"epoch": ep, "verb": "dispatch", "glyph": glyph,
                        "issue": f"#{issue}", "target": target,
                        "detail": f"attempt {attempt}/3"})
        continue
    rm = REAP_RE.search(rest)
    if rm:
        issue, pid = rm.groups()
        for lane, v in list(inflight.items()):
            if v["pid"] == pid:
                del inflight[lane]
        recents.append({"epoch": ep, "verb": "reap", "glyph": "♻",
                        "issue": f"#{issue}", "target": "",
                        "detail": "stale lock + assignee swept"})
        continue
    zm = ZOMBIE_RE.search(rest)
    if zm:
        issue, pid, det = zm.groups()
        for lane, v in list(inflight.items()):
            if v["pid"] == pid:
                del inflight[lane]
        det = det.strip(" —")
        recents.append({"epoch": ep, "verb": "zombie", "glyph": "💀",
                        "issue": f"#{issue}", "target": "", "detail": det})
        continue
    am = ALERT_RE.search(rest)
    if am:
        recents.append({"epoch": ep, "verb": "alert", "glyph": "⚠",
                        "issue": "", "target": "", "detail": am.group(1)})

# Take last 5, newest first.
recents = recents[-5:][::-1]

# ───── reason-tag parsing ─────
REASON_TABLE = [
    ("🛡", "dependency gate"),
    ("🔐", "missing creds"),
    ("💳", "quota / billing"),
    ("❓", "ambiguous AC"),
    ("⚙",  "infra / tooling"),
    ("⏭", "skipped"),
]
def reason_for(n):
    body = reasons.get(str(n)) or ""
    for em, txt in REASON_TABLE:
        if em in body:
            return em, txt
    return "🚫", "other"

# ───── rendering helpers ─────
def visual_width(s):
    """Approximate east-asian-width-aware cell count."""
    w = 0
    for c in s:
        if unicodedata.category(c) == "Mn" or ord(c) == 0xFE0F:
            continue
        if unicodedata.east_asian_width(c) in ("W","F"):
            w += 2
        elif ord(c) >= 0x2600:  # most symbol/emoji blocks render wide
            w += 2
        else:
            w += 1
    return w

def vpad(s, width):
    """Right-pad s with spaces to occupy `width` visual cells."""
    diff = width - visual_width(s)
    if diff > 0:
        return s + (" " * diff)
    return s

def truncate_to(s, width):
    """Truncate s to <= width visual cells, adding … if shortened."""
    if visual_width(s) <= width:
        return s
    out = ""
    for c in s:
        if visual_width(out + c) > width - 1:
            return out + "…"
        out += c
    return out

def box_top(label, count):
    head = f"┌─ {label:<8} [{count}] "
    fill = "─" * (80 - visual_width(head) - 1)
    return head + fill + "┐"

def box_bot():
    return "└" + ("─" * 78) + "┘"

def box_line(body):
    body = truncate_to(body, 76)
    return f"│ {vpad(body, 76)} │"

# ───── header ─────
proj = cfg["project"]
mode_label = "human-approves" if cfg.get("human_approves_merge") else "auto-merge"
tg = cfg.get("truth_gate", "off")
tt = cfg.get("truth_threshold", 70)
gate_label = {"off":"off", "always":"always"}.get(tg, f"non-trivial (≥{tt})")
bot_login = (cfg.get("notifications", {}).get("bot_identity")
             or cfg.get("claim", {}).get("assignee_login") or "?")
max_workers = cfg.get("parallelism", {}).get("max_concurrent_workers", 3)

print(f"📊 super-board · {proj['title']} (#{proj['number']})")
print("─" * 80)
print(f"config: {slug}   variant: {cfg.get('variant','?')}   base: {cfg.get('base_branch','?')}")
print(f"mode:   {mode_label:<22} truth gate: {gate_label}")
print()

# ───── kanban ─────
def glyph_for_issue(n):
    for lane, v in inflight.items():
        if v["issue"] == str(n):
            return {"build":"🔨","qa":"🔍","review":"✏️"}[lane]
    return "  "

def rebuild_suffix(item):
    for lab in item["labels"]:
        m = re.match(r"loop:rebuild-(\d+)", lab)
        if m:
            k = int(m.group(1))
            return f"↻ {min(k + 1, 3)}/3"
    return ""

def render_lane(label, lane_items):
    out = [box_top(label, len(lane_items))]
    if not lane_items:
        out.append(box_line("(empty)"))
    else:
        for it in lane_items:
            n = it["number"]
            glyph = glyph_for_issue(n)
            left = f"{glyph} #{n}  {it['title']}"
            suffix = rebuild_suffix(it)
            if suffix:
                # Right-justify suffix within 76 cells.
                pad = 76 - visual_width(left) - visual_width(suffix)
                if pad < 1: pad = 1
                # Build body, truncate the title if needed to fit suffix.
                budget = 76 - visual_width(suffix) - 1
                if visual_width(left) > budget:
                    left = truncate_to(left, budget)
                    pad = 76 - visual_width(left) - visual_width(suffix)
                    if pad < 1: pad = 1
                out.append(box_line(left + (" " * pad) + suffix))
            else:
                out.append(box_line(left))
    out.append(box_bot())
    return "\n".join(out)

print(render_lane("Ready",    by_status["Ready"]))
print(render_lane("Building", by_status["Building"]))
print(render_lane("QA",       by_status["QA"]))
print(render_lane("Review",   by_status["Review"]))

# Done: single collapsed line.
done = by_status["Done"]
print(box_top("Done", len(done)))
if not done:
    print(box_line("(empty)"))
else:
    nums = [f"#{x['number']}" for x in done]
    tail = "   (squash-merged, collapsed)"
    full = " ".join(nums) + tail
    if visual_width(full) <= 76:
        print(box_line(full))
    else:
        # Fit as many leading numbers as possible, then "… +N more".
        accum = []
        for i, x in enumerate(nums):
            remaining = len(nums) - i - 1
            candidate = " ".join(accum + [x])
            proposed = candidate + (f" … +{remaining} more" if remaining > 0 else "") + tail
            if visual_width(proposed) > 76:
                break
            accum.append(x)
        remaining = len(nums) - len(accum)
        body = " ".join(accum) + (f" … +{remaining} more" if remaining > 0 else "") + tail
        print(box_line(body))
print(box_bot())

# Blocked / Skipped — body shows reason glyph + #N + title.
def render_blocklane(label, lane_items):
    out = [box_top(label, len(lane_items))]
    if not lane_items:
        out.append(box_line("(empty)"))
    else:
        for it in lane_items:
            em, _ = reason_for(it["number"])
            out.append(box_line(f"{em} #{it['number']}  {it['title']}"))
    out.append(box_bot())
    return "\n".join(out)

print(render_blocklane("Blocked", by_status["Blocked"]))
print(render_blocklane("Skipped", by_status["Skipped"]))

# ───── workers section ─────
print()
active = len(inflight)
run_active = bool(last_tick) and not exited

def worker_dur(hms):
    ep = hms_to_epoch(hms); d = now - ep
    if d < 3600:  return f"{d // 60}m"
    if d < 86400: return f"{d // 3600}h"
    return f"{d // 86400}d"

if not run_active and active == 0:
    print(f"▎Workers  (claim: {bot_login})")
    print("   (no active run — `super-board run` to start)")
else:
    print(f"▎Workers  (claim: {bot_login} · {active}/{max_workers} active)")
    if active == 0:
        print("   (idle)")
    else:
        order = {"build": 0, "qa": 1, "review": 2}
        for lane in sorted(inflight, key=lambda l: order.get(l, 9)):
            v = inflight[lane]
            glyph = {"build":"🔨","qa":"🔍","review":"✏️"}[lane]
            role = {"build":"Builder ","qa":"Tester  ","review":"Reviewer"}[lane]
            # Find issue labels for extras (loop:rebuild-N etc.) excluding loop:in-*.
            item = next((i for i in items if i["number"] == int(v["issue"])), None)
            extras = []
            if item:
                extras = [l for l in item["labels"]
                          if l.startswith("loop:") and not l.startswith("loop:in-")]
            extra = (" · " + ", ".join(extras)) if extras else ""
            print(f"   {glyph} {role}  #{v['issue']}  attempt {v['attempt']}/3 · "
                  f"{worker_dur(v['ts'])}{extra}")

# ───── block reasons section ─────
print()
print("▎Block reasons")
blockers = by_status["Blocked"] + by_status["Skipped"]
if not blockers:
    print("   (none)")
else:
    groups = {}
    for it in blockers:
        em, txt = reason_for(it["number"])
        g = groups.setdefault(em, {"text": txt, "issues": []})
        g["issues"].append(f"#{it['number']}")
    for em, g in sorted(groups.items(), key=lambda kv: -len(kv[1]["issues"])):
        issues_str = ", ".join(g["issues"])
        print(f"   {em} ×{len(g['issues'])}  {g['text']:<18}  {issues_str}")

# ───── recent events ─────
print()
print("▎Recent  (last 5 manifest events)")
if not recents:
    print("   (no manifest events yet)")
else:
    def t_minus(ep):
        d = now - ep
        if d < 60:    return "T-1m" if d >= 30 else "T-0m"
        if d < 3600:  return f"T-{d // 60}m"
        if d < 86400: return f"T-{d // 3600}h"
        return f"T-{d // 86400}d"
    for r in recents:
        t = t_minus(r["epoch"])
        if r["target"]:
            print(f"   {t:<7} {r['glyph']} {r['verb']:<9} {r['issue']:<4} → "
                  f"{r['target']:<9} {r['detail']}")
        else:
            print(f"   {t:<7} {r['glyph']} {r['verb']:<9} {r['issue']:<4}   "
                  f"        {r['detail']}")

# ───── health ─────
print()
print("▎Health")
def delta_ago(hms):
    if not hms: return "?"
    ep = hms_to_epoch(hms); d = now - ep
    if d < 90:    return f"{d}s ago"
    if d < 5400:  return f"{d // 60}m ago"
    if d < 86400: return f"{d // 3600}h {(d % 3600) // 60}m ago"
    return f"{d // 86400}d ago"

if run_active:
    print(f"   last tick: {delta_ago(last_tick)}    run started: {delta_ago(start_hms)}"
          f"    workers: {active}/{max_workers}    worktrees cleaned: {reaped_count}")
else:
    if exited and start_hms:
        # last tick under §H = "completed N ago"
        print(f"   last run: completed {delta_ago(last_tick or start_hms)}    "
              f"workers: 0/{max_workers} idle")
    else:
        print(f"   no run today    workers: 0/{max_workers} idle")
PYEOF
