#!/usr/bin/env python3
"""super-board-status.py — read-only live snapshot of the super-board pipeline.

Replaces the model-rendered status path in
`.claude/skills/super-board/references/status.md`. Same output, much faster
because rendering moves from token-by-token generation into a single Python
process (no Anthropic round-trip on the box-drawing).

What it does:
  1. Resolve config slug: arg | `.claude/super-board/active` | sole config.
  2. ONE GraphQL call for project items (number, title, labels, Status).
  3. ONE `gh issue view` per Blocked/Skipped card for reason-tag extraction.
  4. Read today's manifest and pipe everything to the locked-template
     renderer that prints the 80-col snapshot matching the spec in
     references/status.md.

What it does NOT do:
  - Any GitHub mutations (read-only verb — same forbidden set as the skill).
  - Touch the manifest, locks, or worktrees.
  - Wait for or poll workers.

Cross-platform: pure Python 3 stdlib + `gh` CLI. Works on macOS, Linux,
Windows (PowerShell / CMD / Git Bash / WSL). No bash, no jq.

Usage:
  python .claude/bin/super-board-status.py [<config-slug>]

Exit codes:
  0  ok
  64 missing arg + no active marker + no single config
  66 config not found
  67 gh / network failure
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

# Make box-drawing chars render on Windows consoles too.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


# ───────────────────────────── args + paths ─────────────────────────────


def resolve_config_slug(argv: list[str]) -> str:
    if len(argv) > 1 and argv[1]:
        return argv[1]
    active = Path(".claude/super-board/active")
    if active.is_file():
        return active.read_text().strip()
    cfgs = sorted(Path(".claude/super-board/configs").glob("*.json"))
    if len(cfgs) == 1:
        return cfgs[0].stem
    print(f"usage: {argv[0]} <config-slug>  (or set .claude/super-board/active)", file=sys.stderr)
    sys.exit(64)


def gh(*args: str, check: bool = True) -> str:
    """Run gh and return stdout. Exits 67 on failure when check=True."""
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        print("gh CLI not found on PATH", file=sys.stderr)
        sys.exit(67)
    if proc.returncode != 0:
        if check:
            print(f"gh call failed ({' '.join(args[:3])}…)", file=sys.stderr)
            if proc.stderr.strip():
                print(proc.stderr.strip(), file=sys.stderr)
            sys.exit(67)
        return ""
    return proc.stdout


CONFIG_SLUG = resolve_config_slug(sys.argv)
CONFIG_PATH = Path(f".claude/super-board/configs/{CONFIG_SLUG}.json")
if not CONFIG_PATH.is_file():
    print(f"config not found: {CONFIG_PATH}", file=sys.stderr)
    sys.exit(66)

cfg = json.loads(CONFIG_PATH.read_text())
PROJECT_OWNER: str = cfg["project"]["owner"]
PROJECT_NUMBER: int = int(cfg["project"]["number"])
RUNS_DIR = cfg.get("paths", {}).get("runs_dir", "docs/super-board/runs")
RUN_DATE = datetime.date.today().isoformat()
MANIFEST_PATH = Path(RUNS_DIR) / f"{RUN_DATE}-{CONFIG_SLUG}.md"


# ───────────────────────────── GitHub: project items ─────────────────────────────
# Targeted GraphQL — number, title, labels, Status only. ~3 KB vs. 100+ KB for
# `gh project item-list --format json` (which slurps every issue body).

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

items_raw_stdout = gh(
    "api", "graphql",
    "-f", f"query={ITEMS_QUERY}",
    "-F", f"owner={PROJECT_OWNER}",
    "-F", f"number={PROJECT_NUMBER}",
)
items_payload = json.loads(items_raw_stdout)
if items_payload.get("errors"):
    print("graphql returned errors:", file=sys.stderr)
    for err in items_payload["errors"]:
        print(f"  - {err.get('message')}", file=sys.stderr)
    sys.exit(67)

items_raw: list[dict[str, Any]] = (
    items_payload.get("data", {})
    .get("user", {})
    .get("projectV2", {})
    .get("items", {})
    .get("nodes", [])
) or []


def field_status(node: dict[str, Any]) -> str:
    for fv in (node.get("fieldValues", {}).get("nodes") or []):
        if fv and fv.get("field", {}).get("name") == "Status":
            return fv.get("name") or "Backlog"
    return "Backlog"


items: list[dict[str, Any]] = []
for n in items_raw:
    c = n.get("content") or {}
    if not c.get("number"):
        continue
    items.append({
        "number": c["number"],
        "title": c.get("title") or "",
        "labels": [l["name"] for l in (c.get("labels", {}).get("nodes") or []) if l.get("name")],
        "status": field_status(n),
    })

by_status: dict[str, list[dict[str, Any]]] = {
    s: sorted([i for i in items if i["status"] == s], key=lambda x: -x["number"])
    for s in ("Ready", "Building", "QA", "Review", "Done", "Blocked", "Skipped")
}


# ───────────────────────────── reason-tag fetch (Blocked + Skipped only) ─────────
# For each Blocked/Skipped issue, grab its latest comment so the renderer can
# pick a reason emoji from the locked vocabulary. Skip silently on error so a
# stale token doesn't break the rest of the snapshot.

reasons: dict[str, str] = {}
for it in by_status["Blocked"] + by_status["Skipped"]:
    n = it["number"]
    out = gh("issue", "view", str(n), "--json", "comments", check=False)
    if not out:
        continue
    try:
        comments = json.loads(out).get("comments") or []
    except json.JSONDecodeError:
        continue
    comments.sort(key=lambda c: c.get("createdAt", ""))
    body = (comments[-1].get("body") if comments else "") or ""
    reasons[str(n)] = body[:4000]


# ───────────────────────────── manifest read ─────────────────────────────

manifest = MANIFEST_PATH.read_text() if MANIFEST_PATH.is_file() else ""
NOW_EPOCH = int(time.time())
TODAY = RUN_DATE


# ───────────────────────────── manifest parse ─────────────────────────────

TS_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\] (.*)$")
DISPATCH_RE = re.compile(r"dispatch lane=([a-z]+) issue=#(\d+) pid=(\d+).*attempt=(\d+)/3")
REAP_RE = re.compile(r"reaped stale lock.*on #(\d+) \(pid=(\d+)\)")
ZOMBIE_RE = re.compile(r"zombie [a-z]+ worker on #(\d+) \(pid=(\d+)\)(.*)$")
ALERT_RE = re.compile(r"block-rate alert: (.+)$")


def hms_to_epoch(hms: str) -> int:
    try:
        dt = datetime.datetime.strptime(f"{TODAY} {hms}", "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp())
    except Exception:
        return 0


inflight: dict[str, dict[str, str]] = {}
recents: list[dict[str, Any]] = []
last_tick: str | None = None
start_hms: str | None = None
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
        start_hms = hms
        exited = False
        continue
    if "exiting cleanly" in rest:
        exited = True
        continue
    if rest.startswith("tick "):
        last_tick = hms
        continue
    if rest.startswith("reaped worktree"):
        reaped_count += 1
        continue

    if dm := DISPATCH_RE.search(rest):
        lane, issue, pid, attempt = dm.groups()
        inflight[lane] = {"pid": pid, "issue": issue, "attempt": attempt, "ts": hms}
        glyph = {"build": "🔨", "qa": "🔍", "review": "✏️"}[lane]
        target = {"build": "Building", "qa": "QA", "review": "Review"}[lane]
        recents.append({
            "epoch": ep, "verb": "dispatch", "glyph": glyph,
            "issue": f"#{issue}", "target": target,
            "detail": f"attempt {attempt}/3",
        })
        continue
    if rm := REAP_RE.search(rest):
        issue, pid = rm.groups()
        for lane, v in list(inflight.items()):
            if v["pid"] == pid:
                del inflight[lane]
        recents.append({
            "epoch": ep, "verb": "reap", "glyph": "♻",
            "issue": f"#{issue}", "target": "",
            "detail": "stale lock + assignee swept",
        })
        continue
    if zm := ZOMBIE_RE.search(rest):
        issue, pid, det = zm.groups()
        for lane, v in list(inflight.items()):
            if v["pid"] == pid:
                del inflight[lane]
        recents.append({
            "epoch": ep, "verb": "zombie", "glyph": "💀",
            "issue": f"#{issue}", "target": "", "detail": det.strip(" —"),
        })
        continue
    if am := ALERT_RE.search(rest):
        recents.append({
            "epoch": ep, "verb": "alert", "glyph": "⚠",
            "issue": "", "target": "", "detail": am.group(1),
        })

# Take last 5, newest first.
recents = recents[-5:][::-1]


# ───────────────────────────── reason-tag parsing ─────────────────────────────

REASON_TABLE: list[tuple[str, str]] = [
    ("🛡", "dependency gate"),
    ("🔐", "missing creds"),
    ("💳", "quota / billing"),
    ("❓", "ambiguous AC"),
    ("⚙",  "infra / tooling"),
    ("⏭", "skipped"),
]


def reason_for(n: int) -> tuple[str, str]:
    body = reasons.get(str(n)) or ""
    for em, txt in REASON_TABLE:
        if em in body:
            return em, txt
    return "🚫", "other"


# ───────────────────────────── rendering helpers ─────────────────────────────


def visual_width(s: str) -> int:
    """Approximate east-asian-width-aware cell count."""
    w = 0
    for c in s:
        if unicodedata.category(c) == "Mn" or ord(c) == 0xFE0F:
            continue
        if unicodedata.east_asian_width(c) in ("W", "F"):
            w += 2
        elif ord(c) >= 0x2600:  # most symbol/emoji blocks render wide
            w += 2
        else:
            w += 1
    return w


def vpad(s: str, width: int) -> str:
    """Right-pad s with spaces to occupy `width` visual cells."""
    diff = width - visual_width(s)
    return s + (" " * diff) if diff > 0 else s


def truncate_to(s: str, width: int) -> str:
    """Truncate s to <= width visual cells, adding … if shortened."""
    if visual_width(s) <= width:
        return s
    out = ""
    for c in s:
        if visual_width(out + c) > width - 1:
            return out + "…"
        out += c
    return out


def box_top(label: str, count: int) -> str:
    head = f"┌─ {label:<8} [{count}] "
    fill = "─" * (80 - visual_width(head) - 1)
    return head + fill + "┐"


def box_bot() -> str:
    return "└" + ("─" * 78) + "┘"


def box_line(body: str) -> str:
    body = truncate_to(body, 76)
    return f"│ {vpad(body, 76)} │"


# ───────────────────────────── header ─────────────────────────────

proj = cfg["project"]
mode_label = "human-approves" if cfg.get("human_approves_merge") else "auto-merge"
tg = cfg.get("truth_gate", "off")
tt = cfg.get("truth_threshold", 70)
gate_label = {"off": "off", "always": "always"}.get(tg, f"non-trivial (≥{tt})")
bot_login = (
    cfg.get("notifications", {}).get("bot_identity")
    or cfg.get("claim", {}).get("assignee_login")
    or "?"
)
max_workers = cfg.get("parallelism", {}).get("max_concurrent_workers", 3)

print(f"📊 super-board · {proj['title']} (#{proj['number']})")
print("─" * 80)
print(f"config: {CONFIG_SLUG}   variant: {cfg.get('variant', '?')}   base: {cfg.get('base_branch', '?')}")
print(f"mode:   {mode_label:<22} truth gate: {gate_label}")
print()


# ───────────────────────────── kanban ─────────────────────────────


def glyph_for_issue(n: int) -> str:
    for lane, v in inflight.items():
        if v["issue"] == str(n):
            return {"build": "🔨", "qa": "🔍", "review": "✏️"}[lane]
    return "  "


def rebuild_suffix(item: dict[str, Any]) -> str:
    for lab in item["labels"]:
        if m := re.match(r"loop:rebuild-(\d+)", lab):
            k = int(m.group(1))
            return f"↻ {min(k + 1, 3)}/3"
    return ""


def render_lane(label: str, lane_items: list[dict[str, Any]]) -> str:
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
                budget = 76 - visual_width(suffix) - 1
                if visual_width(left) > budget:
                    left = truncate_to(left, budget)
                pad = 76 - visual_width(left) - visual_width(suffix)
                if pad < 1:
                    pad = 1
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
        accum: list[str] = []
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


def render_blocklane(label: str, lane_items: list[dict[str, Any]]) -> str:
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


# ───────────────────────────── workers section ─────────────────────────────

print()
active = len(inflight)
run_active = bool(last_tick) and not exited


def worker_dur(hms: str) -> str:
    ep = hms_to_epoch(hms)
    d = NOW_EPOCH - ep
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
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
            glyph = {"build": "🔨", "qa": "🔍", "review": "✏️"}[lane]
            role = {"build": "Builder ", "qa": "Tester  ", "review": "Reviewer"}[lane]
            item = next((i for i in items if i["number"] == int(v["issue"])), None)
            extras: list[str] = []
            if item:
                extras = [
                    l for l in item["labels"]
                    if l.startswith("loop:") and not l.startswith("loop:in-")
                ]
            extra = (" · " + ", ".join(extras)) if extras else ""
            print(
                f"   {glyph} {role}  #{v['issue']}  attempt {v['attempt']}/3 · "
                f"{worker_dur(v['ts'])}{extra}"
            )


# ───────────────────────────── block reasons section ─────────────────────────────

print()
print("▎Block reasons")
blockers = by_status["Blocked"] + by_status["Skipped"]
if not blockers:
    print("   (none)")
else:
    groups: dict[str, dict[str, Any]] = {}
    for it in blockers:
        em, txt = reason_for(it["number"])
        g = groups.setdefault(em, {"text": txt, "issues": []})
        g["issues"].append(f"#{it['number']}")
    for em, g in sorted(groups.items(), key=lambda kv: -len(kv[1]["issues"])):
        issues_str = ", ".join(g["issues"])
        print(f"   {em} ×{len(g['issues'])}  {g['text']:<18}  {issues_str}")


# ───────────────────────────── recent events ─────────────────────────────

print()
print("▎Recent  (last 5 manifest events)")
if not recents:
    print("   (no manifest events yet)")
else:
    def t_minus(ep: int) -> str:
        d = NOW_EPOCH - ep
        if d < 60:
            return "T-1m" if d >= 30 else "T-0m"
        if d < 3600:
            return f"T-{d // 60}m"
        if d < 86400:
            return f"T-{d // 3600}h"
        return f"T-{d // 86400}d"

    for r in recents:
        t = t_minus(r["epoch"])
        if r["target"]:
            print(
                f"   {t:<7} {r['glyph']} {r['verb']:<9} {r['issue']:<4} → "
                f"{r['target']:<9} {r['detail']}"
            )
        else:
            print(
                f"   {t:<7} {r['glyph']} {r['verb']:<9} {r['issue']:<4}   "
                f"        {r['detail']}"
            )


# ───────────────────────────── health ─────────────────────────────

print()
print("▎Health")


def delta_ago(hms: str | None) -> str:
    if not hms:
        return "?"
    ep = hms_to_epoch(hms)
    d = NOW_EPOCH - ep
    if d < 90:
        return f"{d}s ago"
    if d < 5400:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h {(d % 3600) // 60}m ago"
    return f"{d // 86400}d ago"


if run_active:
    print(
        f"   last tick: {delta_ago(last_tick)}    run started: {delta_ago(start_hms)}"
        f"    workers: {active}/{max_workers}    worktrees cleaned: {reaped_count}"
    )
else:
    if exited and start_hms:
        print(
            f"   last run: completed {delta_ago(last_tick or start_hms)}    "
            f"workers: 0/{max_workers} idle"
        )
    else:
        print(f"   no run today    workers: 0/{max_workers} idle")

# ───── sentry hint (locked one-liner) ─────
# Deliberately quiet — just enough to remind that constant updates are an
# option. The leading two-space indent + en-dash bullet keeps it visually
# subordinate to the `▎Health` line above.
print()
print("   – tip · /super-board sentry for live alerts and a 15-min heartbeat")
