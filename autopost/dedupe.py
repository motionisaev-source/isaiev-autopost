"""Duplicate gate. Runs right before every publish, so the same post never goes out twice on a network.

Two sources of truth are checked:
  1. the ledger `posts/_ledger.json` — every publication autopost has made (plus anything added by hand),
     one entry per post slug + network, with the caption "hook" (first 80 chars, lowercased,
     no emoji, no #tags). Entries are also rebuilt from each post's published.json, so a lost
     ledger can never let an old post through again.
  2. for Threads accounts, the account itself: its posts from the last 48 hours via the Threads API.

A network is skipped (DUPLICATE BLOCKED) when the same slug was already published there, or when the
first 40 characters of the hook are equal to an earlier post's. `allow_repost: true` in post.yaml
(top level, or inside one network's block) turns the gate off for an intentional repost.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import unicodedata

LEDGER_FILE = "_ledger.json"
HOOK_LEN = 80
MATCH_LEN = 40
RECENT_HOURS = 48

_TAG = re.compile(r"[#＃]\w+")
_DROP_CATEGORIES = {"So", "Sk", "Cf", "Cs", "Co", "Cn"}  # emoji, symbols, joiners, private use
_DROP_CHARS = {"︎", "️", "⃣"}  # emoji variation selectors, keycap


def hook(text: str) -> str:
    """Normalized opening of a caption: first 80 chars, lowercased, without emoji or #tags."""
    t = _TAG.sub(" ", unicodedata.normalize("NFKC", text or ""))
    t = "".join(ch for ch in t if ch not in _DROP_CHARS and unicodedata.category(ch) not in _DROP_CATEGORIES)
    return re.sub(r"\s+", " ", t).strip().lower()[:HOOK_LEN]


def hooks_match(a: str, b: str) -> bool:
    """Same opening: first 40 chars equal (or the whole hook, when a caption is shorter than that)."""
    if not a or not b:
        return False
    if len(a) >= MATCH_LEN and len(b) >= MATCH_LEN:
        return a[:MATCH_LEN] == b[:MATCH_LEN]
    return a == b


def allow_repost(post, network: str) -> bool:
    return bool(post.data.get("allow_repost") or (post.data.get(network) or {}).get("allow_repost"))


class Ledger:
    """posts/_ledger.json: list of {slug, network, hook, at, id, url, source}."""

    def __init__(self, posts_dir: pathlib.Path, posts=()):
        self.path = pathlib.Path(posts_dir) / LEDGER_FILE
        self.entries: list[dict] = []
        if self.path.exists():
            self.entries = json.loads(self.path.read_text(encoding="utf-8")).get("entries", [])
        # anything recorded in a post's published.json counts too (covers posts published before the ledger)
        known = {(e.get("slug"), e.get("network")) for e in self.entries}
        for post in posts:
            for net, st in (post.state or {}).items():
                if isinstance(st, dict) and st.get("status") == "published" and (post.name, net) not in known:
                    try:
                        h = hook(post.text(net))
                    except Exception:  # noqa: BLE001 — a broken post.yaml must not stop the gate
                        h = ""
                    self.entries.append({"slug": post.name, "network": net, "hook": h, "at": st.get("at"),
                                         "id": st.get("id"), "url": st.get("url"), "source": "published.json"})

    def find(self, slug: str, network: str, h: str):
        for e in self.entries:
            if e.get("network") != network:
                continue
            if e.get("slug") == slug:
                return e, "same post slug"
            if hooks_match(h, e.get("hook", "")):
                return e, "same caption opening"
        return None

    def add(self, slug: str, network: str, h: str, **info) -> None:
        self.entries = [e for e in self.entries if not (e.get("slug") == slug and e.get("network") == network)]
        self.entries.append({"slug": slug, "network": network, "hook": h, **info})

    def save(self) -> None:
        entries = sorted(self.entries, key=lambda e: (str(e.get("at") or ""), e.get("slug", ""), e.get("network", "")))
        self.path.write_text(json.dumps({"_note": "Published ledger, used by the duplicate gate (autopost/dedupe.py). "
                                                  "Add an entry by hand for a post made outside autopost.",
                                         "entries": entries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _exact(text: str, have: str) -> bool:
    """The account already holds this very text (an earlier run published it, then lost its state)."""
    from .networks import _norm
    want, got = _norm(text)[:100], _norm(have)
    return len(want) >= 20 and len(got) >= 20 and (got.startswith(want) or want.startswith(got[:100]))


def check(post, network: str, publisher, ledger: Ledger):
    """None = go ahead. ("adopt", info) = this exact post is already live, record it as published.
    ("blocked", info) = a duplicate, skip this network."""
    if allow_repost(post, network):
        return None
    text = post.text(network)
    h = hook(text)
    found = ledger.find(post.name, network, h)
    if found:
        e, why = found
        return "blocked", {"reason": f"{why} as {e.get('slug')} ({e.get('at') or '?'})", "duplicate_of": e.get("url") or e.get("slug")}
    recent = getattr(publisher, "recent_posts", None)
    if recent:
        for item in recent(RECENT_HOURS):
            have = item.get("text") or ""
            if _exact(text, have):
                return "adopt", {"id": item.get("id"), "url": item.get("permalink"), "note": "already on the account, not posted again"}
            if hooks_match(h, hook(have)):
                return "blocked", {"reason": f"same caption opening as a post on the account ({item.get('timestamp') or '?'})",
                                   "duplicate_of": item.get("permalink") or item.get("id")}
    return None


def parse_ts(raw: str):
    try:
        return dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return None
