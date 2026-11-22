import datetime as dt
import json
import pathlib
import sys

import pytest
import yaml
from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from autopost import __main__ as cli  # noqa: E402
from autopost import networks  # noqa: E402
from autopost.core import due_jobs, load_posts  # noqa: E402


class Resp:
    def __init__(self, body=None, status=200, headers=None):
        self._b, self.status_code, self.headers = body or {}, status, headers or {}
        self.text = json.dumps(self._b)

    def json(self):
        return self._b


def make_post(root, name, when, extra=None, imgs=2):
    d = root / name
    d.mkdir(parents=True)
    names = []
    for i in range(imgs):
        n = f"s{i + 1}.png"
        Image.new("RGB", (1080, 1350), (20, 20, 20)).save(d / n)
        names.append(n)
    data = {"publish_at": when, "images": names,
            "instagram": {"caption": "Hi #ai"}, "threads": {"text": "Thread (test)"},
            "linkedin": {"text": "Post (with) #tags & @mention"}}
    data.update(extra or {})
    (d / "post.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return d


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in {"IG_USER_ID": "1", "IG_ACCESS_TOKEN": "t", "THREADS_ACCESS_TOKEN": "t",
                 "LINKEDIN_ACCESS_TOKEN": "t", "LINKEDIN_AUTHOR_URN": "urn:li:person:x",
                 "MEDIA_BASE_URL": "https://example.com/repo", "AUTOPOST_POLL_SECONDS": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(networks, "POLL_EVERY", 0)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    return tmp_path


def test_due_logic(tmp_path):
    make_post(tmp_path / "posts", "past", "2026-10-29 10:00")
    make_post(tmp_path / "posts", "future", "2026-10-29 18:00")
    make_post(tmp_path / "posts", "draft", "2026-10-29 09:00", {"draft": True})
    now = dt.datetime(2026, 10, 29, 9, 30, tzinfo=dt.timezone.utc)  # 10:30 Rome
    jobs = due_jobs(load_posts(tmp_path / "posts"), now)
    assert {(p.name, n) for p, n in jobs} == {("past", "instagram"), ("past", "threads"), ("past", "linkedin")}


def test_full_run(env, monkeypatch):
    posts = env / "posts"
    make_post(posts, "p1", "2020-01-01 10:00")
    calls = []

    def fake_post(url, data=None, json=None, headers=None, timeout=None, files=None):
        calls.append(("POST", url, data or json))
        if url.endswith("/rest/images?action=initializeUpload"):
            return Resp({"value": {"uploadUrl": "https://upload", "image": f"urn:li:image:{len(calls)}"}})
        if url.endswith("/rest/posts"):
            return Resp({}, 201, {"x-restli-id": "urn:li:share:9"})
        if url.endswith("_publish"):
            return Resp({"id": "pub1"})
        return Resp({"id": f"c{len(calls)}"})

    def fake_get(url, params=None, timeout=None, headers=None):
        f = (params or {}).get("fields", "")
        if f == "permalink":
            return Resp({"permalink": "https://x/p"})
        return Resp({"status_code": "FINISHED", "status": "FINISHED"})

    monkeypatch.setattr(networks.requests, "post", fake_post)
    monkeypatch.setattr(networks.requests, "get", fake_get)
    monkeypatch.setattr(networks.requests, "put", lambda *a, **k: Resp({}, 201))

    assert cli.cmd_prepare(posts) == 0
    assert (posts / "p1" / "s1.jpg").exists()
    assert cli.cmd_run(posts, dry=False) == 0
    state = json.loads((posts / "p1" / "published.json").read_text())
    assert all(state[n]["status"] == "published" for n in ("instagram", "threads", "linkedin"))
    ig_items = [c for c in calls if "graph.instagram.com" in c[1] and c[2].get("is_carousel_item")]
    assert ig_items and ig_items[0][2]["image_url"].endswith("/posts/p1/s1.jpg")
    li = [c for c in calls if c[1].endswith("/rest/posts")][0][2]
    assert li["commentary"] == r"Post \(with\) \#tags & \@mention"
    assert len(li["content"]["multiImage"]["images"]) == 2
    # second run: nothing is re-posted
    calls.clear()
    assert cli.cmd_run(posts, dry=False) == 0 and calls == []


def test_retry_then_fail(env, monkeypatch):
    posts = env / "posts"
    make_post(posts, "p2", "2020-01-01 10:00", {"threads": None, "linkedin": None})
    monkeypatch.setattr(networks.requests, "post", lambda *a, **k: Resp({"error": {"message": "boom"}}, 500))
    cli.cmd_prepare(posts)
    for i in range(3):
        assert cli.cmd_run(posts, dry=False) == 1
    st = json.loads((posts / "p2" / "published.json").read_text())["instagram"]
    assert st["status"] == "failed" and st["attempts"] == 3
    assert cli.cmd_run(posts, dry=False) == 0  # gives up, no more retries


def test_validation_errors(env):
    posts = env / "posts"
    make_post(posts, "p3", "2020-01-01 10:00", {"threads": {"text": "x" * 501}, "linkedin": None})
    assert cli.cmd_check(posts) == 1


def test_threads_vasyl_optin_and_fallback(tmp_path):
    from autopost.core import load_posts
    d = tmp_path / "pv"; d.mkdir()
    (d / "post.yaml").write_text(
        'publish_at: "2020-01-01 10:00"\nthreads:\n  text: "Hello"\nthreads_vasyl: {}\n', encoding="utf-8")
    (tmp_path / "po").mkdir()
    (tmp_path / "po" / "post.yaml").write_text(
        'publish_at: "2020-01-01 10:00"\nthreads:\n  text: "Only main"\n', encoding="utf-8")
    posts = {p.name: p for p in load_posts(tmp_path)}
    assert "threads_vasyl" in posts["pv"].networks()
    assert posts["pv"].text("threads_vasyl") == "Hello"
    assert "threads_vasyl" not in posts["po"].networks()


def _yaml(d, text):
    d.mkdir(parents=True)
    (d / "post.yaml").write_text(text, encoding="utf-8")


def test_vasyl_auto_mirror_for_new_posts_only(tmp_path):
    _yaml(tmp_path / "old", 'threads:\n  publish_at: "2026-11-21 08:30"\n  text: "Old post"\n')
    _yaml(tmp_path / "new", 'threads:\n  publish_at: "2026-11-23 08:30"\n  text: "New post"\n')
    _yaml(tmp_path / "off", 'threads:\n  publish_at: "2026-11-23 08:30"\n  text: "No mirror"\nthreads_vasyl:\n  enabled: false\n')
    posts = {p.name: p for p in load_posts(tmp_path)}
    assert "threads_vasyl" not in posts["old"].networks()
    assert "threads_vasyl" in posts["new"].networks()
    assert posts["new"].publish_at("threads_vasyl") == posts["new"].publish_at("threads")
    assert "threads_vasyl" not in posts["off"].networks()


def test_vasyl_backlog_one_per_hour(tmp_path):
    for i in range(3):
        _yaml(tmp_path / f"b{i}", f'threads:\n  publish_at: "2026-11-01 10:00"\n  text: "T{i}"\n'
                                  f'  enabled: false\nthreads_vasyl:\n  publish_at: "2026-11-22 13:0{i}"\n  text: "T{i}"\n')
    now = dt.datetime(2026, 11, 22, 13, 0, tzinfo=dt.timezone.utc)
    posts = load_posts(tmp_path)
    assert [p.name for p, n in due_jobs(posts, now)] == ["b0"]
    posts[0].state["threads_vasyl"] = {"status": "published", "at": now.isoformat()}
    assert due_jobs(posts, now + dt.timedelta(minutes=30)) == []
    assert [p.name for p, n in due_jobs(posts, now + dt.timedelta(minutes=60))] == ["b1"]


def test_threads_duplicate_guard(env, monkeypatch):
    posts = env / "posts"
    make_post(posts, "dup", "2020-01-01 10:00", {"instagram": None, "linkedin": None,
                                                   "threads": {"text": "Light first. Everything else second. mela.team #Marketing"}}, imgs=0)
    created = []
    monkeypatch.setattr(networks.requests, "post", lambda url, **k: created.append(url) or Resp({"id": "x"}))
    monkeypatch.setattr(networks.requests, "get", lambda url, params=None, timeout=None: Resp(
        {"data": [{"id": "77", "text": "Light first. Everything else second. mela.team Marketing", "permalink": "https://t/77"}]}))
    assert cli.cmd_run(posts, dry=False) == 0
    st = json.loads((posts / "dup" / "published.json").read_text())["threads"]
    assert st["status"] == "published" and st["id"] == "77" and created == []


def test_missing_secret_waits(env, monkeypatch):
    posts = env / "posts"
    make_post(posts, "w", "2020-01-01 10:00", {"instagram": None, "linkedin": None,
                                                 "threads": {"text": "Hello world, this is a test post"}, "threads_vasyl": {}}, imgs=0)
    monkeypatch.delenv("THREADS_VASYL_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(networks.requests, "post", lambda url, **k: Resp({"id": "x"}))
    monkeypatch.setattr(networks.requests, "get", lambda url, params=None, timeout=None: Resp({"data": [], "status": "FINISHED"}))
    cli.cmd_run(posts, dry=False)
    st = json.loads((posts / "w" / "published.json").read_text())
    assert st["threads"]["status"] == "published"
    assert "status" not in st["threads_vasyl"] and "attempts" not in st["threads_vasyl"]
