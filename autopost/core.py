"""Isaiev AI autopost: loads post folders, decides what is due, keeps state."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
from zoneinfo import ZoneInfo

import yaml

DEFAULT_TZ = os.environ.get("AUTOPOST_TZ", "Europe/Rome")
NETWORKS = ("instagram", "threads", "threads_vasyl", "linkedin", "telegram")
# Extra accounts reuse the main block's content when their own block leaves it out.
FALLBACK = {"threads_vasyl": "threads"}
MAX_ATTEMPTS = int(os.environ.get("AUTOPOST_MAX_ATTEMPTS", "3"))
STATE_FILE = "published.json"


class PostError(Exception):
    """A problem with the post itself (bad config, too long text...). Not retried."""


@dataclasses.dataclass
class Post:
    folder: pathlib.Path
    data: dict
    state: dict

    @property
    def name(self) -> str:
        return self.folder.name

    @property
    def draft(self) -> bool:
        return bool(self.data.get("draft", False))

    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.data.get("timezone", DEFAULT_TZ))

    def publish_at(self, network: str) -> dt.datetime:
        raw = (self.data.get(network) or {}).get("publish_at") or self.data.get("publish_at")
        if not raw:
            raise PostError(f"{self.name}: missing publish_at")
        if isinstance(raw, dt.datetime):
            when = raw
        else:
            when = dt.datetime.fromisoformat(str(raw).strip().replace(" ", "T", 1))
        if when.tzinfo is None:
            when = when.replace(tzinfo=self.tz())
        return when

    def networks(self) -> list[str]:
        return [n for n in NETWORKS if isinstance(self.data.get(n), dict) and self.data[n].get("enabled", True)]

    def _cfg(self, network: str) -> dict:
        cfg = dict(self.data.get(network) or {})
        base = FALLBACK.get(network)
        if base:
            merged = dict(self.data.get(base) or {})
            merged.pop("publish_at", None)
            merged.pop("enabled", None)
            merged.update(cfg)
            cfg = merged
        return cfg

    def text(self, network: str) -> str:
        cfg = self._cfg(network)
        return str(cfg.get("text") or cfg.get("caption") or self.data.get("text") or "").strip()

    def images(self, network: str) -> list[pathlib.Path]:
        cfg = self._cfg(network)
        names = cfg.get("images", self.data.get("images", [])) or []
        paths = [self.folder / n for n in names]
        missing = [p.name for p in paths if not p.exists()]
        if missing:
            raise PostError(f"{self.name}: image files not found: {', '.join(missing)}")
        return paths

    def video(self, network: str):
        """Optional single video (yaml key `video`, per network or top level). None if not set."""
        cfg = self._cfg(network)
        name = cfg.get("video", self.data.get("video"))
        if not name:
            return None
        path = self.folder / name
        if not path.exists():
            raise PostError(f"{self.name}: video file not found: {name}")
        return path

    def alt(self, network: str) -> str:
        cfg = self._cfg(network)
        return str(cfg.get("alt") or self.data.get("alt") or "")

    def option(self, network: str, key: str, default=None):
        return (self.data.get(network) or {}).get(key, default)

    # ---- state -------------------------------------------------------
    def net_state(self, network: str) -> dict:
        return self.state.setdefault(network, {})

    def is_done(self, network: str) -> bool:
        s = self.net_state(network)
        return s.get("status") in ("published", "failed")

    def save_state(self) -> None:
        (self.folder / STATE_FILE).write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def load_posts(root: pathlib.Path) -> list[Post]:
    posts = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        cfg = folder / "post.yaml"
        if not cfg.exists():
            continue
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        state_path = folder / STATE_FILE
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        posts.append(Post(folder=folder, data=data, state=state))
    return posts


def due_jobs(posts: list[Post], now: dt.datetime) -> list[tuple[Post, str]]:
    jobs = []
    for post in posts:
        if post.draft:
            continue
        for net in post.networks():
            if post.is_done(net):
                continue
            if post.publish_at(net) <= now:
                jobs.append((post, net))
    return jobs


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
