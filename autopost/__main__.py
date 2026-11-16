"""CLI:  python -m autopost [check|prepare|run] [--dry-run] [--posts DIR]"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import traceback
import urllib.parse

from PIL import Image

from .core import MAX_ATTEMPTS, PostError, due_jobs, load_posts, now_utc
from .networks import ApiError, Instagram, LinkedIn, Telegram, Threads

ROOT = pathlib.Path(__file__).resolve().parent.parent


def media_url(post, path) -> str:
    base = os.environ.get("MEDIA_BASE_URL", "").rstrip("/")
    if not base:
        repo = os.environ.get("GITHUB_REPOSITORY")
        branch = os.environ.get("GITHUB_REF_NAME", "main")
        if not repo:
            raise PostError("MEDIA_BASE_URL is not set (needed for Instagram/Threads image URLs)")
        base = f"https://raw.githubusercontent.com/{repo}/{branch}"
    rel = path.resolve().relative_to(ROOT).as_posix()
    return f"{base}/{urllib.parse.quote(rel)}"


PUBLISHERS = {
    "instagram": lambda: Instagram(media_url),
    "threads": lambda: Threads(media_url),
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


def cmd_run(posts_dir: pathlib.Path, dry: bool) -> int:
    jobs = due_jobs(load_posts(posts_dir), now_utc())
    if not jobs:
        print("nothing due")
        return 0
    failed = 0
    lines = ["### Isaiev AI autopost", ""]
    for post, net in jobs:
        st = post.net_state(net)
        if dry:
            lines.append(f"- [dry-run] {post.name} → {net}")
            continue
        try:
            result = PUBLISHERS[net]().publish(post)
            st.update(status="published", at=now_utc().isoformat(timespec="seconds"), **result)
            st.pop("last_error", None)
            lines.append(f"- ✅ {post.name} → {net} {result.get('url') or ''}")
        except PostError as e:
            st.update(status="failed", last_error=str(e))
            failed += 1
            lines.append(f"- ❌ {post.name} → {net}: {e}")
        except (ApiError, Exception) as e:  # noqa: BLE001 — record and retry later
            st["attempts"] = st.get("attempts", 0) + 1
            st["last_error"] = f"{type(e).__name__}: {e}"
            if st["attempts"] >= MAX_ATTEMPTS:
                st["status"] = "failed"
            failed += 1
            lines.append(f"- ⚠️ {post.name} → {net} (спроба {st['attempts']}/{MAX_ATTEMPTS}): {e}")
            traceback.print_exc()
        post.save_state()
    summary(lines)
    return 1 if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="autopost")
    ap.add_argument("command", choices=["check", "prepare", "run"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--posts", default=str(ROOT / "posts"))
    args = ap.parse_args(argv)
    posts_dir = pathlib.Path(args.posts).resolve()
    if args.command == "prepare":
        return cmd_prepare(posts_dir)
    if args.command == "check":
        return cmd_check(posts_dir)
    return cmd_run(posts_dir, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
