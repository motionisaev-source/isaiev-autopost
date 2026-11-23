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


def test_next_wait(tmp_path, capsys, monkeypatch):
    _yaml(tmp_path / "a", 'threads:\n  enabled: false\n  text: "A"\nthreads_vasyl:\n  publish_at: "2026-11-22 13:00"\n  text: "A"\n')
    _yaml(tmp_path / "b", 'threads:\n  enabled: false\n  text: "B"\nthreads_vasyl:\n  publish_at: "2026-11-22 13:01"\n  text: "B"\n')
    (tmp_path / "a" / "published.json").write_text(json.dumps({"threads_vasyl": {"status": "published", "at": "2026-11-22T12:30:00+00:00"}}))
    monkeypatch.setattr(cli, "now_utc", lambda: dt.datetime(2026, 11, 22, 12, 45, tzinfo=dt.timezone.utc))
    cli.cmd_next(tmp_path)
    assert capsys.readouterr().out.strip() == str(40 * 60)
    (tmp_path / "b" / "published.json").write_text(json.dumps({"threads_vasyl": {"last_error": "secret X is not set"}}))
    cli.cmd_next(tmp_path)
    assert capsys.readouterr().out.strip() == ""


# ------------------------------------------------------------ duplicate gate
from autopost import dedupe  # noqa: E402


def test_hook_normalization():
    assert dedupe.hook("🔥 SERPENS:  a bracelet ✨ film #AI #Jewelry") == "serpens: a bracelet film"
    assert len(dedupe.hook("x" * 200)) == 80
    assert dedupe.hooks_match("a" * 40 + "one", "a" * 40 + "two")
    assert not dedupe.hooks_match("short one", "short two") and not dedupe.hooks_match("", "")


def _text_post(posts, name, text, **extra):
    return make_post(posts, name, "2020-01-01 10:00",
                     {"instagram": None, "linkedin": None, "threads": {"text": text}, **extra}, imgs=0)


def _fake_threads(monkeypatch, on_account=()):
    created = []
    monkeypatch.setattr(networks.requests, "post", lambda url, **k: created.append(url) or Resp({"id": "new1"}))

    def get(url, params=None, timeout=None):
        if (params or {}).get("fields") == "id,text,permalink,timestamp":
            return Resp({"data": list(on_account)})
        if (params or {}).get("fields") == "permalink":
            return Resp({"permalink": "https://t/new1"})
        return Resp({"status": "FINISHED"})
    monkeypatch.setattr(networks.requests, "get", get)
    return created


SERPENS = "SERPENS: a bracelet film with nothing but metal and light. Macro, metal, light. mela.team"


def test_gate_blocks_same_hook_on_account(env, monkeypatch, capsys):
    posts = env / "posts"
    _text_post(posts, "s2", "✨ SERPENS: a bracelet film with nothing but... a new edit, other words #AI")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
    created = _fake_threads(monkeypatch, [{"id": "1", "text": SERPENS, "permalink": "https://t/1", "timestamp": now}])
    assert cli.cmd_run(posts, dry=False) == 0
    st = json.loads((posts / "s2" / "published.json").read_text())["threads"]
    assert st["status"] == "duplicate_blocked" and st["duplicate_of"] == "https://t/1" and created == []
    out = capsys.readouterr().out
    assert "DUPLICATE BLOCKED" in out and "| s2 | threads |" in out
    assert cli.cmd_run(posts, dry=False) == 0 and created == []  # stays blocked, not retried


def test_gate_ignores_account_posts_older_than_48h(env, monkeypatch):
    posts = env / "posts"
    _text_post(posts, "s3", SERPENS + " again")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    created = _fake_threads(monkeypatch, [{"id": "1", "text": "SERPENS: a bracelet film with nothing but X", "timestamp": old}])
    assert cli.cmd_run(posts, dry=False) == 0
    assert json.loads((posts / "s3" / "published.json").read_text())["threads"]["status"] == "published" and created


def test_gate_ledger_blocks_other_slug_and_records(env, monkeypatch):
    posts = env / "posts"
    _text_post(posts, "a1", SERPENS)
    created = _fake_threads(monkeypatch)
    assert cli.cmd_run(posts, dry=False) == 0 and created
    ledger = json.loads((posts / dedupe.LEDGER_FILE).read_text(encoding="utf-8"))["entries"]
    assert [(e["slug"], e["network"], e["source"]) for e in ledger] == [("a1", "threads", "autopost")]
    # a second folder with the same opening, on a network with no API history (telegram): ledger alone blocks it
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@c")
    _text_post(posts, "a2", SERPENS.upper(), threads=None, telegram={"text": SERPENS + " #reels"})
    (posts / dedupe.LEDGER_FILE).write_text(json.dumps({"entries": ledger + [
        {"slug": "manual", "network": "telegram", "hook": dedupe.hook(SERPENS), "source": "by hand"}]}), encoding="utf-8")
    created.clear()
    assert cli.cmd_run(posts, dry=False) == 0 and created == []
    assert json.loads((posts / "a2" / "published.json").read_text())["telegram"]["status"] == "duplicate_blocked"


def test_gate_same_slug_after_lost_state(env, monkeypatch):
    posts = env / "posts"
    _text_post(posts, "lost", "Totally new text that nobody has seen before, really.")
    (posts / dedupe.LEDGER_FILE).write_text(json.dumps({"entries": [
        {"slug": "lost", "network": "threads", "hook": "something else", "url": "https://t/9"}]}), encoding="utf-8")
    created = _fake_threads(monkeypatch)
    assert cli.cmd_run(posts, dry=False) == 0 and created == []
    st = json.loads((posts / "lost" / "published.json").read_text())["threads"]
    assert st["status"] == "duplicate_blocked" and "same post slug" in st["reason"]


def test_allow_repost_override(env, monkeypatch):
    posts = env / "posts"
    _text_post(posts, "re", SERPENS, allow_repost=True)
    (posts / dedupe.LEDGER_FILE).write_text(json.dumps({"entries": [
        {"slug": "old", "network": "threads", "hook": dedupe.hook(SERPENS)}]}), encoding="utf-8")
    created = _fake_threads(monkeypatch, [{"id": "1", "text": SERPENS}])
    assert cli.cmd_run(posts, dry=False) == 0 and created
    assert json.loads((posts / "re" / "published.json").read_text())["threads"]["status"] == "published"


def test_dry_run_reports_block_without_writing(env, monkeypatch, capsys):
    posts = env / "posts"
    _text_post(posts, "d1", SERPENS)
    (posts / dedupe.LEDGER_FILE).write_text(json.dumps({"entries": [
        {"slug": "x", "network": "threads", "hook": dedupe.hook(SERPENS)}]}), encoding="utf-8")
    created = _fake_threads(monkeypatch)
    assert cli.cmd_run(posts, dry=True) == 0 and created == []
    assert "DUPLICATE BLOCKED" in capsys.readouterr().out
    assert not (posts / "d1" / "published.json").exists()


def test_delete_queue(env, monkeypatch, capsys):
    posts = env / "posts"
    _text_post(posts, "dd", SERPENS)
    (posts / "dd" / "published.json").write_text(json.dumps({"threads": {"status": "published", "id": "55", "url": "https://www.threads.com/@a/post/ABC"}}))
    (posts / cli.DELETE_FILE).write_text(json.dumps({"delete": [
        {"network": "threads", "id": "55", "permalink": "https://www.threads.com/@a/post/ABC", "slug": "dd"},
        {"network": "threads", "id": "66", "permalink": "https://www.threads.com/@a/post/OTHER"}]}))
    deleted = []
    monkeypatch.setattr(networks.requests, "get", lambda url, params=None, timeout=None: Resp(
        {"permalink": "https://www.threads.net/@a/post/ABC/" if url.endswith("/55") else "https://www.threads.net/@a/post/ZZZ"}))
    monkeypatch.setattr(networks.requests, "delete", lambda url, params=None, timeout=None: deleted.append(url) or Resp({"success": True}))
    assert cli.cmd_run(posts, dry=False) == 1  # the mismatched id is refused
    assert deleted == ["https://graph.threads.net/v1.0/55"]
    q = json.loads((posts / cli.DELETE_FILE).read_text())["delete"]
    assert q[0]["status"] == "deleted" and "nothing deleted" in q[1]["last_error"]
    assert json.loads((posts / "dd" / "published.json").read_text())["threads"]["status"] == "deleted_duplicate"
    deleted.clear()
    cli.cmd_run(posts, dry=False)
    assert deleted == []  # done once; the post is never re-published either
