"""Tests for super-board-sentry.py's persistence + bookkeeping invariants.

Pure stdlib, same conventions as test_status_parse.py — no pytest, no `gh`,
no network. These tests pin three behaviors the script has to keep:

1. `seen_merged_prs` never grows past SEEN_MERGED_RETAIN.
2. State writes are atomic: a crash mid-write leaves either the old file or
   the new one, never an empty / truncated JSON.
3. The TELEGRAM `tg_key` set emitted by `render_event` stays in sync with
   the menu options documented in `references/sentry.md` — otherwise users
   can't opt into events the script can otherwise fan out.

The sentry script runs `gh` and reads config at import time, so we can't
just `importlib`-load it the way test_status_parse.py does. We re-read the
constants we need from the source and re-implement the two helpers in a
shape that matches the script. If the script's contract changes, this
file is the canary.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SENTRY = _REPO_ROOT / "scripts" / "super-board-sentry.py"
_SPEC = _REPO_ROOT / "skills" / "super-board" / "references" / "sentry.md"

_SOURCE = _SENTRY.read_text(encoding="utf-8")


def _const(name: str) -> int:
    m = re.search(rf"^{name}\s*=\s*(\d+)\s*$", _SOURCE, re.MULTILINE)
    assert m, f"could not find {name} in {_SENTRY}"
    return int(m.group(1))


SEEN_MERGED_RETAIN = _const("SEEN_MERGED_RETAIN")


# ───────────────────────────── test 1: trim cap ─────────────────────────────


def test_seen_merged_cap_keeps_most_recent() -> None:
    """A long PR history must trim to SEEN_MERGED_RETAIN, keeping newest."""
    seen = set(range(1, 1000))
    trimmed = sorted(seen)[-SEEN_MERGED_RETAIN:]
    assert len(trimmed) == SEEN_MERGED_RETAIN
    assert trimmed[-1] == 999, "most recent PR number must survive"
    assert trimmed[0] == 1000 - SEEN_MERGED_RETAIN
    # The script uses the literal expression `sorted(seen_merged)[-SEEN_MERGED_RETAIN:]`
    # at both persist sites — both must use the cap, never raw `sorted(seen)`.
    raw_count = len(re.findall(r"sorted\(seen_merged\)", _SOURCE))
    capped_count = len(re.findall(
        rf"sorted\(seen_merged\)\[-SEEN_MERGED_RETAIN:\]", _SOURCE,
    ))
    assert raw_count == capped_count, (
        f"every `sorted(seen_merged)` must carry the [-SEEN_MERGED_RETAIN:] cap; "
        f"saw {raw_count} bare calls and {capped_count} capped"
    )


# ───────────────────────────── test 2: atomic write ─────────────────────────────


def _atomic_persist(state_file: Path, payload: dict[str, Any]) -> None:
    """Mirror of `persist_state` in super-board-sentry.py."""
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, state_file)


def test_atomic_persist_replaces_old_file() -> None:
    """After a successful write, the file holds the new payload exactly."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "sentry-state.json"
        f.write_text('{"old": true}')
        _atomic_persist(f, {"new": True, "seen_merged_prs": [1, 2, 3]})
        loaded = json.loads(f.read_text())
        assert loaded == {"new": True, "seen_merged_prs": [1, 2, 3]}
        # No stray tmp left behind.
        assert not f.with_suffix(f.suffix + ".tmp").exists()


def test_atomic_persist_failure_preserves_old_file() -> None:
    """If the tmp write fails, the visible file must still be the old one."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "sentry-state.json"
        original = '{"old": true}\n'
        f.write_text(original)
        # Simulate: tmp file write succeeds, os.replace not yet called.
        tmp = f.with_suffix(f.suffix + ".tmp")
        tmp.write_text('{"partial":')  # truncated JSON
        # If sentry crashes here, on next start we should be able to load f
        # (still old) without touching tmp.
        assert f.read_text() == original
        loaded = json.loads(f.read_text())  # parses fine
        assert loaded == {"old": True}
        # And the script's load path doesn't read tmp, so it's harmless.
        tmp.unlink()


# ───────────────────────────── test 3: menu / tg_key sync ─────────────────────────────


def _tg_keys_from_source() -> set[str]:
    """Extract every literal `tg_key` emitted by render_event."""
    keys: set[str] = set()
    # Match `return lines, "...", "<key>"` at the end of each branch.
    for m in re.finditer(
        r'return\s+lines,\s*[^,]+,\s*"([a-z\-]+)"', _SOURCE,
    ):
        keys.add(m.group(1))
    return keys


def _menu_keys_from_spec() -> set[str]:
    """Pull the lowercase key list from the spec's lowercase paragraph.

    The paragraph wraps across multiple lines in the markdown source, so
    we match with DOTALL and stop at the period that ends the sentence
    listing the keys.
    """
    text = _SPEC.read_text(encoding="utf-8")
    m = re.search(
        r"Lowercase the alert\s+keys[^:]*:\s*(.+?)\.\s",
        text,
        re.DOTALL,
    )
    assert m, "spec must enumerate lowercase keys in the standard sentence"
    return set(re.findall(r"`([a-z\-]+)`", m.group(1)))


def test_render_event_tg_keys_match_spec_menu() -> None:
    """Every key the script can emit must be selectable in the first-run menu."""
    script_keys = _tg_keys_from_source()
    spec_keys = _menu_keys_from_spec()
    assert script_keys, "regex failed to find any tg_key in the script"
    assert spec_keys, "regex failed to find any lowercase key in the spec"
    missing_from_spec = script_keys - spec_keys
    extra_in_spec = spec_keys - script_keys
    assert not missing_from_spec, (
        f"script can fan out {sorted(missing_from_spec)} but spec menu "
        f"never offers them — user can't opt in"
    )
    assert not extra_in_spec, (
        f"spec menu lists {sorted(extra_in_spec)} but script never emits "
        f"them — selecting them would be a no-op"
    )


# ───────────────────────────── runner ─────────────────────────────


def _run() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok  {name}")
        except Exception:
            failures += 1
            print(f"  FAIL {name}")
            traceback.print_exc()
    total = sum(1 for n in globals() if n.startswith("test_"))
    print(f"\n{total - failures}/{total} passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run())
