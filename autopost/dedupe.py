"""Duplicate gate. Runs right before every publish, so the same post never goes out twice on a network.

Two sources of truth are checked:
  1. the ledger `posts/_ledger.json` — every publication autopost has made (plus anything added by hand),
     one entry per post slug + network, with the caption "hook" (first 80 chars, lowercased,
     no emoji, no #tags). Entries are also rebuilt from each post's published.json, so a lost
     ledger can never let an old post through again.
  2. for Threads accounts, the account itself: its posts from the last 48 hours via the Threads API.

A network is skipped (DUPLICATE BLOCKED) when the same slug was already published there, when the
first 40 characters of the hook are equal to an earlier post's, or when it is the same case: the post's
`case` (post.yaml, else inferred from the slug: 2026-11-23_salt_mirror -> salt-mirror; series slugs with
digits get none) matches an earlier post's. On Threads the account's last 48 h also counts as the same
case when a post there names the case or shares 3+ distinctive words with our hook (both openings); this catches
Instagram's automatic "Share to Threads" copies, which carry the Instagram caption, not ours. `allow_repost: true` in post.yaml
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


# words that say nothing about which case a caption is about
_STOP = set("""
about above after again also another because been before behind being below between both brand built
case cases come comment could does down each else even every film films follow frame frames from have
here into just like made make many mela more most much need never next only other over project prompt
reel reels same send shot shots should some spec such take team than that their them then there these
they thing this those through very video videos want week well were what when where which while will
with would year your fictional instagram threads link bio
della delle dello degli sono come questo questa nella nelle anche perché più dove quando tutto tutti
який яка яке які щоб тому цей ця це для коли тут там після перед дуже лише також
""".split())
_WORD = re.compile(r"[^\W\d_]{4,}")


def words(text: str) -> set:
    """Distinctive words of a caption (4+ letters, no #tags, no common words)."""
    t = _TAG.sub(" ", unicodedata.normalize("NFKC", text or "").lower())
    return {w for w in _WORD.findall(t) if w not in _STOP}


def case_of(post=None, slug: str = "") -> str:
    """The case a post belongs to: post.yaml `case`, else the slug without its date.
    Series slugs with numbers (italy05, mela_post07) have no case: their posts share words on purpose."""
    raw = (post.data.get("case") if post is not None else None) or ""
    if not raw:
        name = re.sub(r"^\d{4}-\d{2}-\d{2}_", "", slug or (post.name if post is not None else ""))
        if re.search(r"\d", name) or name.startswith("_"):
            return ""
        raw = name
    return re.sub(r"[\s_]+", "-", str(raw).strip().lower()).strip("-")


def mentions_case(case: str, text: str) -> bool:
    if not case:
        return False
    pat = r"(?<![^\W_])" + r"[\s_-]*".join(map(re.escape, case.split("-"))) + r"(?![^\W_])"
    return re.search(pat, unicodedata.normalize("NFKC", text or "").lower()) is not None


def similar(ours: str, theirs: str, case: str = ""):
    """Why `theirs` is the same post as `ours`, or None."""
    if hooks_match(hook(ours), hook(theirs)):
        return "same caption opening"
    if mentions_case(case, theirs):
        return f"names the case '{case}'"
    shared = words(hook(ours)) & words(hook(theirs))  # openings only: whole captions share topic words too often
    if len(shared) >= 3:
        return "same case, shared words: " + ", ".join(sorted(shared)[:6])
    return None


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
                    self.entries.append({"slug": post.name, "network": net, "hook": h, "case": case_of(post), "at": st.get("at"),
                                         "id": st.get("id"), "url": st.get("url"), "source": "published.json"})

    def find(self, slug: str, network: str, h: str, case: str = ""):
        for e in self.entries:
            if e.get("network") != network:
                continue
            if e.get("slug") == slug:
                return e, "same post slug"
            if hooks_match(h, e.get("hook", "")):
                return e, "same caption opening"
            if case and (e.get("case") or case_of(slug=e.get("slug", ""))) == case:
                return e, f"same case '{case}'"
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
    case = case_of(post)
    found = ledger.find(post.name, network, h, case)
    if found:
        e, why = found
        return "blocked", {"reason": f"{why} as {e.get('slug')} ({e.get('at') or '?'})", "duplicate_of": e.get("url") or e.get("slug")}
    recent = getattr(publisher, "recent_posts", None)
    if recent:
        for item in recent(RECENT_HOURS):
            have = item.get("text") or ""
            if _exact(text, have):
                return "adopt", {"id": item.get("id"), "url": item.get("permalink"), "note": "already on the account, not posted again"}
            why = similar(text, have, case)
            if why:
                return "blocked", {"reason": f"{why}, as a post on the account ({item.get('timestamp') or '?'})",
                                   "duplicate_of": item.get("permalink") or item.get("id")}
    return None


def parse_ts(raw: str):
    try:
        return dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return None


def find_account_duplicates(items: list[dict], queued=()) -> list[tuple[dict, dict, str]]:
    """Pairs (kept, newer copy, why) among one account's recent posts. The newer copy is the one to remove.
    A pair is skipped when either post is already queued for deletion (e.g. the owner chose to drop the older one)."""
    def ts(item):
        t = parse_ts(item.get("timestamp"))
        return t.timestamp() if t else 0.0
    posts = sorted((i for i in items if (i.get("text") or "").strip()), key=ts)
    out, gone = [], {str(q) for q in queued}
    for n, newer in enumerate(posts):
        if str(newer.get("id")) in gone:
            continue
        for older in posts[:n]:
            if str(older.get("id")) in gone:
                continue
            a, b = older.get("text", ""), newer.get("text", "")
            why = similar(a, b) or similar(b, a)
            if why:
                out.append((older, newer, why))
                gone.add(str(newer.get("id")))
                break
    return out
