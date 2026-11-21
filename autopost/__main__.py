"""CLI:  python -m autopost [check|prepare|run|next|ledger] [--dry-run] [--posts DIR]"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import traceback
import urllib.parse

from PIL import Image

from . import dedupe
from .core import MAX_ATTEMPTS, MIN_GAP_MINUTES, PostError, _last_published, due_jobs, load_posts, now_utc
from .networks import ApiError, NotConfigured, Facebook, Instagram, LinkedIn, Telegram, Threads, ThreadsVasyl

ROOT = pathlib.Path(__file__).resolve().parent.parent


def media_url(post, path) -> str:
    base = os.environ.get("MEDIA_BASE_URL", "").rstrip("/")
    if not base:
        repo = os.environ.get("GITHUB_REPOSITORY")
        branch = os.environ.get("GITHUB_REF_NAME", "main")
        if not repo:
            raise PostError("MEDIA_BASE_URL is not set (needed for Instagram/Facebook/Threads media URLs)")
        base = f"https://raw.githubusercontent.com/{repo}/{branch}"
    rel = path.resolve().relative_to(ROOT).as_posix()
    return f"{base}/{urllib.parse.quote(rel)}"


PUBLISHERS = {
    "instagram": lambda: Instagram(media_url),
    "facebook": lambda: Facebook(media_url),
    "threads": lambda: Threads(media_url),
    "threads_vasyl": lambda: ThreadsVasyl(media_url),
    "linkedin": LinkedIn,
    "telegram": Telegram,
}


def summary(lines: list[str]) -> None:
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def cmd_prepare(posts_dir: pathlib.Path) -> int:
    converted = 0
    for img in posts_dir.glob("*/*"):
        if img.suffix.lower() in (".png", ".webp") and not img.with_suffix(".jpg").exists():
            with Image.open(img) as im:
                bg = Image.new("RGB", im.size, (0, 0, 0))
                rgba = im.convert("RGBA")
                bg.paste(rgba, mask=rgba.split()[-1])
                bg.save(img.with_suffix(".jpg"), "JPEG", quality=92, optimize=True)
            converted += 1
            print(f"converted {img.relative_to(posts_dir)} -> .jpg")
    print(f"prepare: {converted} image(s) converted")
    return 0


def cmd_check(posts_dir: pathlib.Path) -> int:
    lines, errors = ["| Пост | Мережа | Коли | Стан |", "|---|---|---|---|"], 0
    for post in load_posts(posts_dir):
        for net in post.networks():
            when = "?"
            try:
                when = post.publish_at(net).strftime("%Y-%m-%d %H:%M %Z")
                pub = PUBLISHERS[net]()
                if hasattr(pub, "validate"):
                    pub.validate(post)
                st = post.net_state(net).get("status", "draft" if post.draft else "чекає")
            except PostError as e:
                st, errors = f"ПОМИЛКА: {e}", errors + 1
            lines.append(f"| {post.name} | {net} | {when} | {st} |")
    summary(lines)
    return 1 if errors else 0


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


DELETE_FILE = "_delete.json"


def run_deletions(posts_dir: pathlib.Path, posts, ledger, dry: bool, rows: list) -> int:
    """posts/_delete.json: duplicates to remove, [{network, id, permalink, slug?, reason}].
    Each entry is done once; the post's published.json and the ledger are marked deleted_duplicate,
    so the gate still sees it and autopost never posts it again."""
    path = posts_dir / DELETE_FILE
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    by_name = {p.name: p for p in posts}
    failed = 0
    for item in data.get("delete", []):
        if item.get("status") in ("deleted", "failed", "needs_permission"):
            continue  # done, given up, or waiting for a token with threads_delete (reset status to retry)
        net, mid, label = item.get("network"), str(item.get("id") or ""), item.get("permalink") or item.get("id")
        if dry:
            rows.append((item.get("slug") or "-", net, f"[dry-run] would delete duplicate {label}"))
            continue
        try:
            pub = PUBLISHERS[net]()
            if not hasattr(pub, "delete") or not mid:
                raise PostError(f"cannot delete on {net} (need a Threads network and an id)")
            pub.delete(mid, item.get("permalink", ""))
            item.update(status="deleted", at=now_utc().isoformat(timespec="seconds"))
            item.pop("last_error", None)
            print(f"DUPLICATE DELETED: {net} {label}")
            rows.append((item.get("slug") or "-", net, f"🗑 DUPLICATE DELETED {label}"))
            post = by_name.get(item.get("slug") or "")
            if post and str(post.net_state(net).get("id")) == mid:
                post.net_state(net).update(status="deleted_duplicate", deleted_at=item["at"], reason=item.get("reason", ""))
                post.save_state()
            for e in ledger.entries:
                if e.get("network") == net and str(e.get("id")) == mid:
                    e.update(deleted=item["at"], source=f"{e.get('source', '')}; deleted as duplicate")
            ledger.save()
        except (PostError, ApiError, Exception) as e:  # noqa: BLE001 — report the exact error, keep the entry
            item["last_error"] = f"{type(e).__name__}: {e}"
            if "does not have permission" in str(e):  # token lacks threads_delete: wait, don't fail every run
                item["status"] = "needs_permission"
                print(f"DELETE WAITING: {net} {label}: token has no threads_delete permission")
                rows.append((item.get("slug") or "-", net, f"⏸ delete waits for threads_delete permission {label}"))
                continue
            item["attempts"] = item.get("attempts", 0) + 1
            if item["attempts"] >= MAX_ATTEMPTS:
                item["status"] = "failed"
            failed += 1
            print(f"DELETE FAILED: {net} {label}: {e}")
            rows.append((item.get("slug") or "-", net, f"❌ delete failed {label}: {e}"))
    if not dry:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return failed


def scan_accounts(posts_dir: pathlib.Path, posts, dry: bool, rows: list) -> None:
    """Look at the Threads accounts' last 48 h for copies made outside autopost (e.g. Instagram's automatic
    "Share to Threads"): the newer post of a same-case pair is queued in posts/_delete.json."""
    path = posts_dir / DELETE_FILE
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"delete": []}
    queued = {str(i.get("id")) for i in data.get("delete", [])}
    added = 0
    for net in ("threads", "threads_vasyl"):
        try:
            items = PUBLISHERS[net]().recent_posts(dedupe.RECENT_HOURS)
        except NotConfigured:
            continue
        except Exception as e:  # noqa: BLE001 — a failed scan must never stop publishing
            print(f"scan {net}: {type(e).__name__}: {e}")
            continue
        for kept, copy, why in dedupe.find_account_duplicates(items, queued):
            cid = str(copy.get("id"))
            slug = next((p.name for p in posts if str((p.state.get(net) or {}).get("id")) == cid), "")
            label = copy.get("permalink") or cid
            print(f"DUPLICATE FOUND ON ACCOUNT: {net} {label} ({why}) -> queued for deletion")
            rows.append((slug or "-", net, f"🔎 duplicate on the account, queued for deletion: {label} ({why})"))
            if not dry:
                data.setdefault("delete", []).append({
                    "network": net, "id": cid, "permalink": copy.get("permalink", ""), "slug": slug,
                    "reason": f"{why}; newer copy of {kept.get('permalink') or kept.get('id')}",
                    "found": now_utc().isoformat(timespec="seconds")})
                queued.add(cid)
                added += 1
    if added:
        data.setdefault("_note", "Duplicates to delete. autopost run deletes each once (Threads API), checks the permalink "
                                 "first, then marks it deleted here, in the post's published.json and in _ledger.json.")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_run(posts_dir: pathlib.Path, dry: bool) -> int:
    posts = load_posts(posts_dir)
    ledger = dedupe.Ledger(posts_dir, posts)
    rows = []
    scan_accounts(posts_dir, posts, dry, rows)
    failed = run_deletions(posts_dir, posts, ledger, dry, rows)
    jobs = due_jobs(posts, now_utc())
    if not jobs and not rows:
        print("nothing due")
        return 0
    for post, net in jobs:
        st = post.net_state(net)
        try:
            publisher = PUBLISHERS[net]()
            # duplicate gate: right before the publish, every time
            try:
                verdict = dedupe.check(post, net, publisher, ledger)
            except NotConfigured as e:
                if not dry:
                    raise
                verdict = None
                rows.append((post.name, net, f"[dry-run] account check skipped: {e}"))
            if verdict and verdict[0] == "blocked":
                info = verdict[1]
                print(f"DUPLICATE BLOCKED: {post.name} -> {net}: {info['reason']} {info.get('duplicate_of') or ''}")
                rows.append((post.name, net, f"🛑 DUPLICATE BLOCKED: {info['reason']} {info.get('duplicate_of') or ''}"))
                if not dry:
                    st.update(status="duplicate_blocked", at=now_utc().isoformat(timespec="seconds"),
                              reason=info["reason"], duplicate_of=info.get("duplicate_of"))
                    st.pop("last_error", None)
                    post.save_state()
                continue
            if dry:
                rows.append((post.name, net, "[dry-run] would adopt the post already on the account" if verdict else "[dry-run] would publish"))
                continue
            result = verdict[1] if verdict else publisher.publish(post)
            st.update(status="published", at=now_utc().isoformat(timespec="seconds"), **result)
            st.pop("last_error", None)
            ledger.add(post.name, net, dedupe.hook(post.text(net)), case=dedupe.case_of(post), at=st["at"], id=st.get("id"), url=st.get("url"),
                       source="found_on_account" if verdict else "autopost")
            ledger.save()
            rows.append((post.name, net, f"✅ {result.get('url') or ''} {result.get('note') or ''}".strip()))
        except NotConfigured as e:  # waits for the secret, keeps its place in the queue
            st["last_error"] = str(e)
            rows.append((post.name, net, f"⏸ {e}"))
        except PostError as e:
            st.update(status="failed", last_error=str(e))
            failed += 1
            rows.append((post.name, net, f"❌ {e}"))
        except (ApiError, Exception) as e:  # noqa: BLE001 — record and retry later
            st["attempts"] = st.get("attempts", 0) + 1
            st["last_error"] = f"{type(e).__name__}: {e}"
            if st["attempts"] >= MAX_ATTEMPTS:
                st["status"] = "failed"
            failed += 1
            rows.append((post.name, net, f"⚠️ спроба {st['attempts']}/{MAX_ATTEMPTS}: {e}"))
            traceback.print_exc()
        if not dry:
            post.save_state()
    lines = ["### Isaiev AI autopost", "", "| Пост | Мережа | Результат |", "|---|---|---|"]
    lines += [f"| {_cell(a)} | {_cell(b)} | {_cell(c)} |" for a, b, c in rows]
    summary(lines)
    return 1 if failed else 0


def cmd_ledger(posts_dir: pathlib.Path) -> int:
    """Rebuild posts/_ledger.json from every post's published.json (keeps hand-added entries)."""
    ledger = dedupe.Ledger(posts_dir, load_posts(posts_dir))
    ledger.save()
    print(f"ledger: {len(ledger.entries)} entries -> {ledger.path}")
    return 0


def cmd_next(posts_dir: pathlib.Path) -> int:
    """Seconds until the next throttled (@isaiev_vasyl) post may go out; prints nothing if none waits
    or if the account's secret is still missing. Used by the drip workflow to run once an hour."""
    import datetime as dt
    posts, now = load_posts(posts_dir), now_utc()
    waits = []
    for net, gap in MIN_GAP_MINUTES.items():
        pending = [p for p in posts if not p.draft and net in p.networks() and not p.is_done(net)]
        if not pending or any(("is not set" in e or "expected @" in e) for e in (p.net_state(net).get("last_error", "") for p in pending)):
            continue
        ready = max(min(p.publish_at(net) for p in pending), now)
        last = _last_published(posts, net)
        if last:
            ready = max(ready, last + dt.timedelta(minutes=gap))
        waits.append(int((ready - now).total_seconds()))
    if waits:
        print(max(min(waits), 0))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="autopost")
    ap.add_argument("command", choices=["check", "prepare", "run", "next", "ledger"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--posts", default=str(ROOT / "posts"))
    args = ap.parse_args(argv)
    posts_dir = pathlib.Path(args.posts).resolve()
    if args.command == "prepare":
        return cmd_prepare(posts_dir)
    if args.command == "check":
        return cmd_check(posts_dir)
    if args.command == "next":
        return cmd_next(posts_dir)
    if args.command == "ledger":
        return cmd_ledger(posts_dir)
    return cmd_run(posts_dir, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
