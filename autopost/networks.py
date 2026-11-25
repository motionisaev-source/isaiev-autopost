"""Publishers for each social network. Official APIs only."""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable

import requests

from .core import Post, PostError

TIMEOUT = 60
POLL_EVERY = float(os.environ.get("AUTOPOST_POLL_SECONDS", "5"))
POLL_MAX = int(os.environ.get("AUTOPOST_POLL_TRIES", "36"))  # ~3 min


class ApiError(Exception):
    """Network/API problem. Retried on the next run."""


class NotConfigured(PostError):
    """A secret is missing. The post waits (no attempt is used) until the secret is added."""


def _env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise NotConfigured(f"secret {name} is not set")
    return val


def _norm(text: str) -> str:
    """Text for duplicate detection: Threads turns the last #tag into a topic, so tags are dropped."""
    text = re.sub(r"#\w+", " ", text or "")
    return re.sub(r"\s+", " ", text).strip().lower()


def _check(resp: requests.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:500]}
    if resp.status_code >= 400 or (isinstance(body, dict) and "error" in body and body.get("error")):
        raise ApiError(f"HTTP {resp.status_code}: {str(body)[:800]}")
    return body


def _poll(fn: Callable[[], str], ok: tuple[str, ...], bad: tuple[str, ...], what: str) -> None:
    for _ in range(POLL_MAX):
        status = fn()
        if status in ok:
            return
        if status in bad:
            raise ApiError(f"{what}: processing status {status}")
        time.sleep(POLL_EVERY)
    raise ApiError(f"{what}: still processing after {POLL_MAX * POLL_EVERY:.0f}s")


# ---------------------------------------------------------------- Instagram
class Instagram:
    """Instagram content publishing. Photos (1–10, JPEG) or one video posted as a Reel.

    Two official ways in, chosen by IG_API_HOST:
    - graph.facebook.com (default): Instagram API with Facebook Login. IG_USER_ID is the
      Instagram business account id linked to the Facebook Page; the token is a Page token
      (IG_ACCESS_TOKEN, or FB_PAGE_ACCESS_TOKEN when IG_ACCESS_TOKEN is not set).
    - graph.instagram.com: Instagram API with Instagram Login (needs a business portfolio).
    """

    name = "instagram"
    max_text = 2200

    def __init__(self, media_url: Callable[[Post, object], str]):
        self.media_url = media_url
        self.version = os.environ.get("IG_API_VERSION", "v23.0")
        host = os.environ.get("IG_API_HOST", "").strip() or "graph.facebook.com"
        self.base = f"https://{host}/{self.version}"

    def _user(self) -> str:
        return _env("IG_USER_ID")

    def _token(self) -> str:
        tok = os.environ.get("IG_ACCESS_TOKEN", "").strip() or os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
        if not tok:
            raise NotConfigured("secret IG_ACCESS_TOKEN (or FB_PAGE_ACCESS_TOKEN) is not set")
        return tok

    def _create(self, **params) -> str:
        params["access_token"] = self._token()
        body = _check(requests.post(f"{self.base}/{self._user()}/media", data=params, timeout=TIMEOUT))
        return body["id"]

    def _status(self, cid: str) -> str:
        body = _check(requests.get(f"{self.base}/{cid}", params={"fields": "status_code", "access_token": self._token()}, timeout=TIMEOUT))
        return body.get("status_code", "")

    @staticmethod
    def images(post: Post):
        # PNG/WebP are converted by `prepare` into a .jpg next to them; Instagram takes JPEG only.
        out = []
        for p in post.images("instagram"):
            j = p.with_suffix(".jpg")
            out.append(j if p.suffix.lower() not in (".jpg", ".jpeg") and j.exists() else p)
        return out

    def validate(self, post: Post) -> None:
        video = post.video(self.name)
        if video:
            if video.suffix.lower() not in (".mp4", ".mov"):
                raise PostError("instagram: Reels video must be .mp4 or .mov")
        else:
            imgs = self.images(post)
            if not 1 <= len(imgs) <= 10:
                raise PostError("instagram: needs 1–10 images or one video")
            bad = [p.name for p in imgs if p.suffix.lower() not in (".jpg", ".jpeg")]
            if bad:
                raise PostError(f"instagram: JPEG only, got {', '.join(bad)} (run `python -m autopost prepare`)")
        if len(post.text(self.name)) > self.max_text:
            raise PostError("instagram: caption longer than 2200 characters")
        if post.text(self.name).count("#") > 30:
            raise PostError("instagram: more than 30 hashtags")

    def recent_posts(self, hours: int = 48) -> list[dict]:
        """Own recent posts (also the ones made by hand in the app), read by the duplicate gate."""
        from .dedupe import parse_ts
        since = time.time() - hours * 3600
        body = _check(requests.get(f"{self.base}/{self._user()}/media",
                                   params={"fields": "id,caption,permalink,timestamp", "limit": 50, "access_token": self._token()},
                                   timeout=TIMEOUT))
        out = []
        for item in body.get("data", []):
            ts = parse_ts(item.get("timestamp"))
            if ts is None or ts.timestamp() >= since:
                out.append({"id": item.get("id"), "text": item.get("caption") or "", "permalink": item.get("permalink"),
                            "timestamp": item.get("timestamp")})
        return out

    def _wait(self, cid: str, what: str, rounds: int = 1) -> None:
        for _ in range(rounds):
            try:
                _poll(lambda: self._status(cid), ("FINISHED",), ("ERROR", "EXPIRED"), what)
                return
            except ApiError as e:
                if "still processing" not in str(e):
                    raise
        raise ApiError(f"{what}: still processing")

    def publish(self, post: Post) -> dict:
        self.validate(post)
        caption = post.text(self.name)
        video = post.video(self.name)
        if video:
            params = dict(media_type="REELS", video_url=self.media_url(post, video), caption=caption,
                          share_to_feed="true")
            cover = post.option(self.name, "cover")
            if cover:
                params["cover_url"] = self.media_url(post, post.folder / cover)
            elif post.option(self.name, "thumb_offset") is not None:
                params["thumb_offset"] = str(int(post.option(self.name, "thumb_offset")))
            cid = self._create(**params)
            self._wait(cid, "instagram reel", rounds=5)  # video processing takes minutes
        else:
            imgs = self.images(post)
            if len(imgs) == 1:
                cid = self._create(image_url=self.media_url(post, imgs[0]), caption=caption)
            else:
                children = []
                for img in imgs:
                    child = self._create(image_url=self.media_url(post, img), is_carousel_item="true")
                    self._wait(child, "instagram item")
                    children.append(child)
                cid = self._create(media_type="CAROUSEL", children=",".join(children), caption=caption)
            self._wait(cid, "instagram post")
        body = _check(requests.post(f"{self.base}/{self._user()}/media_publish",
                                    data={"creation_id": cid, "access_token": self._token()}, timeout=TIMEOUT))
        media_id = body["id"]
        result = {"id": media_id}
        try:
            info = _check(requests.get(f"{self.base}/{media_id}", params={"fields": "permalink", "access_token": self._token()}, timeout=TIMEOUT))
            result["url"] = info.get("permalink")
        except ApiError:
            pass
        comment = post.option(self.name, "first_comment")
        if comment:
            try:
                _check(requests.post(f"{self.base}/{media_id}/comments",
                                     data={"message": comment, "access_token": self._token()}, timeout=TIMEOUT))
                result["first_comment"] = "ok"
            except ApiError as e:  # post is already live: never fail the whole job for a comment
                result["first_comment"] = f"failed: {e}"
        return result


# ----------------------------------------------------------------- Facebook
class Facebook:
    """Facebook Page posts via the Graph API (Pages API). Text, 1–10 photos or one video.
    Secrets: FB_PAGE_ID, FB_PAGE_ACCESS_TOKEN (Page token with pages_manage_posts)."""

    name = "facebook"
    max_text = 63206

    def __init__(self, media_url: Callable[[Post, object], str]):
        self.media_url = media_url
        self.base = f"https://graph.facebook.com/{os.environ.get('FB_API_VERSION', 'v23.0')}"

    def _page(self) -> str:
        return _env("FB_PAGE_ID")

    def _token(self) -> str:
        return _env("FB_PAGE_ACCESS_TOKEN")

    def _post(self, edge: str, **params) -> dict:
        params["access_token"] = self._token()
        return _check(requests.post(f"{self.base}/{edge}", data=params, timeout=TIMEOUT * 5))

    def validate(self, post: Post) -> None:
        text, imgs, video = post.text(self.name), post.images(self.name), post.video(self.name)
        if len(text) > self.max_text:
            raise PostError("facebook: text too long")
        if len(imgs) > 10:
            raise PostError("facebook: more than 10 images")
        if not text and not imgs and not video:
            raise PostError("facebook: empty post")

    def recent_posts(self, hours: int = 48) -> list[dict]:
        """The Page's own recent posts, read by the duplicate gate."""
        from .dedupe import parse_ts
        since = time.time() - hours * 3600
        body = _check(requests.get(f"{self.base}/{self._page()}/posts",
                                   params={"fields": "id,message,permalink_url,created_time", "since": int(since), "limit": 50,
                                           "access_token": self._token()}, timeout=TIMEOUT))
        out = []
        for item in body.get("data", []):
            ts = parse_ts(item.get("created_time"))
            if ts is None or ts.timestamp() >= since:
                out.append({"id": item.get("id"), "text": item.get("message") or "", "permalink": item.get("permalink_url"),
                            "timestamp": item.get("created_time")})
        return out

    def _permalink(self, obj_id: str):
        try:
            info = _check(requests.get(f"{self.base}/{obj_id}", params={"fields": "permalink_url", "access_token": self._token()}, timeout=TIMEOUT))
            url = info.get("permalink_url")
            return f"https://www.facebook.com{url}" if url and url.startswith("/") else url
        except ApiError:
            return None

    def publish(self, post: Post) -> dict:
        self.validate(post)
        page, text = self._page(), post.text(self.name)
        imgs, video = self._jpgs(post), post.video(self.name)
        if video:
            body = self._post(f"{page}/videos", file_url=self.media_url(post, video), description=text)
            obj = body["id"]
        elif not imgs:
            link = post.option(self.name, "link")
            body = self._post(f"{page}/feed", message=text, **({"link": link} if link else {}))
            obj = body["id"]
        elif len(imgs) == 1:
            body = self._post(f"{page}/photos", url=self.media_url(post, imgs[0]), caption=text)
            obj = body.get("post_id") or body["id"]
        else:
            ids = [self._post(f"{page}/photos", url=self.media_url(post, p), published="false")["id"] for p in imgs]
            params = {"message": text}
            for i, mid in enumerate(ids):
                params[f"attached_media[{i}]"] = json.dumps({"media_fbid": mid})
            obj = self._post(f"{page}/feed", **params)["id"]
        return {"id": obj, "url": self._permalink(obj)}

    def _jpgs(self, post: Post):
        out = []
        for p in post.images(self.name):
            j = p.with_suffix(".jpg")
            out.append(j if p.suffix.lower() not in (".jpg", ".jpeg") and j.exists() else p)
        return out


# ------------------------------------------------------------------ Threads
class Threads:
    """Threads API (graph.threads.net). Text ≤500 chars, 0–20 images."""

    name = "threads"
    env_prefix = "THREADS"
    max_text = 500

    def __init__(self, media_url: Callable[[Post, object], str]):
        self.media_url = media_url
        self.base = f"https://graph.threads.net/{os.environ.get('THREADS_API_VERSION', 'v1.0')}"

    def _user(self) -> str:
        return os.environ.get(f"{self.env_prefix}_USER_ID", "").strip() or "me"

    def _token(self) -> str:
        return _env(f"{self.env_prefix}_ACCESS_TOKEN")

    def _create(self, **params) -> str:
        params["access_token"] = self._token()
        body = _check(requests.post(f"{self.base}/{self._user()}/threads", data=params, timeout=TIMEOUT))
        return body["id"]

    def _status(self, cid: str) -> str:
        body = _check(requests.get(f"{self.base}/{cid}", params={"fields": "status", "access_token": self._token()}, timeout=TIMEOUT))
        return body.get("status", "")

    def validate(self, post: Post) -> None:
        if len(post.text(self.name)) > self.max_text:
            raise PostError(f"{self.name}: text longer than 500 characters")
        if len(post.images(self.name)) > 20:
            raise PostError(f"{self.name}: more than 20 images")
        if not post.text(self.name) and not post.images(self.name):
            raise PostError(f"{self.name}: empty post")

    def get_post(self, media_id: str) -> dict:
        """One post as it is live now (raises ApiError if it no longer exists)."""
        return _check(requests.get(f"{self.base}/{media_id}", params={
            "fields": "id,text,media_type,permalink,timestamp", "access_token": self._token()}, timeout=TIMEOUT))

    def views(self, media_id: str):
        """Views and likes of a post, or None when insights are not available to this token."""
        try:
            body = _check(requests.get(f"{self.base}/{media_id}/insights", params={
                "metric": "views,likes", "access_token": self._token()}, timeout=TIMEOUT))
        except ApiError:
            return None
        out = {}
        for m in body.get("data", []):
            vals = m.get("values") or [{}]
            out[m.get("name")] = (m.get("total_value") or {}).get("value", vals[0].get("value"))
        return out

    def delete(self, media_id: str, permalink: str = "") -> dict:
        """Delete one post of this account (Threads API, needs threads_delete). When a permalink is given,
        the post's own permalink must end with the same code, so a wrong id can never delete another post."""
        if permalink:
            info = _check(requests.get(f"{self.base}/{media_id}", params={"fields": "permalink", "access_token": self._token()}, timeout=TIMEOUT))
            code = lambda u: (u or "").rstrip("/").rsplit("/", 1)[-1]
            if code(info.get("permalink")) != code(permalink):
                raise PostError(f"{self.name}: id {media_id} is {info.get('permalink')}, not {permalink}; nothing deleted")
        body = _check(requests.delete(f"{self.base}/{media_id}", params={"access_token": self._token()}, timeout=TIMEOUT))
        if isinstance(body, dict) and body.get("success") is False:
            raise ApiError(f"{self.name}: delete refused: {body}")
        return body if isinstance(body, dict) else {"raw": str(body)}

    def recent_posts(self, hours: int = 48) -> list[dict]:
        """The account's own posts from the last `hours` (read by the duplicate gate before every publish)."""
        from .dedupe import parse_ts
        since = time.time() - hours * 3600
        body = _check(requests.get(f"{self.base}/{self._user()}/threads",
                                   params={"fields": "id,text,permalink,timestamp,media_type", "since": int(since), "limit": 100,
                                           "access_token": self._token()}, timeout=TIMEOUT))
        out = []
        for item in body.get("data", []):
            ts = parse_ts(item.get("timestamp"))
            if ts is None or ts.timestamp() >= since:
                out.append(item)
        return out

    def publish(self, post: Post) -> dict:
        self.validate(post)
        imgs = post.images(self.name)
        text = post.text(self.name)
        video = post.video(self.name)
        if video:
            cid = self._create(media_type="VIDEO", video_url=self.media_url(post, video), text=text)
        elif not imgs:
            cid = self._create(media_type="TEXT", text=text)
        elif len(imgs) == 1:
            cid = self._create(media_type="IMAGE", image_url=self.media_url(post, imgs[0]), text=text)
        else:
            children = []
            for img in imgs:
                child = self._create(media_type="IMAGE", image_url=self.media_url(post, img), is_carousel_item="true")
                children.append(child)
            for child in children:
                _poll(lambda c=child: self._status(c), ("FINISHED",), ("ERROR", "EXPIRED"), "threads item")
            cid = self._create(media_type="CAROUSEL", children=",".join(children), text=text)
        if video:  # video containers take longer to process
            for _ in range(4):
                try:
                    _poll(lambda: self._status(cid), ("FINISHED",), ("ERROR", "EXPIRED"), "threads video")
                    break
                except ApiError as e:
                    if "still processing" not in str(e):
                        raise
            else:
                raise ApiError("threads video: still processing")
        else:
            _poll(lambda: self._status(cid), ("FINISHED",), ("ERROR", "EXPIRED"), "threads post")
        body = _check(requests.post(f"{self.base}/{self._user()}/threads_publish",
                                    data={"creation_id": cid, "access_token": self._token()}, timeout=TIMEOUT))
        result = {"id": body["id"]}
        try:
            info = _check(requests.get(f"{self.base}/{body['id']}", params={"fields": "permalink", "access_token": self._token()}, timeout=TIMEOUT))
            result["url"] = info.get("permalink")
        except ApiError:
            pass
        return result


# ----------------------------------------------------------------- LinkedIn
_LI_SPECIAL = re.compile(r"([\\|{}@\[\]()<>#*_~])")


def linkedin_escape(text: str) -> str:
    """Posts API 'little text' format: reserved characters must be backslash-escaped."""
    return _LI_SPECIAL.sub(r"\\\1", text)


class LinkedIn:
    """LinkedIn Posts API. Member profile (w_member_social) or organization page."""

    name = "linkedin"
    max_text = 3000

    def __init__(self):
        self.version = os.environ.get("LINKEDIN_VERSION", "202609")

    def _headers(self, json_body: bool = True) -> dict:
        h = {
            "Authorization": f"Bearer {_env('LINKEDIN_ACCESS_TOKEN')}",
            "LinkedIn-Version": self.version,
            "X-Restli-Protocol-Version": "2.0.0",
        }
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def _author(self, post: Post) -> str:
        author = post.option(self.name, "author") or os.environ.get("LINKEDIN_AUTHOR_URN", "").strip()
        if not author:
            raise PostError("linkedin: set secret LINKEDIN_AUTHOR_URN (urn:li:person:... or urn:li:organization:...)")
        return author

    def validate(self, post: Post) -> None:
        if post.video(self.name):
            raise PostError("linkedin: video posts are not supported yet")
        if len(post.text(self.name)) > self.max_text:
            raise PostError("linkedin: text longer than 3000 characters")
        if len(post.images(self.name)) > 20:
            raise PostError("linkedin: more than 20 images")

    def _upload(self, author: str, path) -> str:
        body = _check(requests.post(
            "https://api.linkedin.com/rest/images?action=initializeUpload",
            json={"initializeUploadRequest": {"owner": author}}, headers=self._headers(), timeout=TIMEOUT))
        value = body["value"]
        put = requests.put(value["uploadUrl"], data=path.read_bytes(),
                           headers={"Authorization": self._headers()["Authorization"]}, timeout=TIMEOUT)
        if put.status_code >= 400:
            raise ApiError(f"linkedin upload HTTP {put.status_code}: {put.text[:300]}")
        return value["image"]

    def publish(self, post: Post) -> dict:
        self.validate(post)
        author = self._author(post)
        imgs = post.images(self.name)
        alt = post.alt(self.name)
        payload = {
            "author": author,
            "commentary": linkedin_escape(post.text(self.name)),
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        urns = [self._upload(author, p) for p in imgs]
        if len(urns) == 1:
            payload["content"] = {"media": {"id": urns[0], "altText": alt}}
        elif len(urns) > 1:
            payload["content"] = {"multiImage": {"images": [{"id": u, "altText": alt} for u in urns]}}
        resp = requests.post("https://api.linkedin.com/rest/posts", json=payload, headers=self._headers(), timeout=TIMEOUT)
        if resp.status_code >= 400:
            raise ApiError(f"linkedin HTTP {resp.status_code}: {resp.text[:800]}")
        urn = resp.headers.get("x-restli-id", "")
        return {"id": urn, "url": f"https://www.linkedin.com/feed/update/{urn}/" if urn else None}


# ----------------------------------------------------------------- Telegram
_TG_LINK_RE = None


def _tg_link_entities(text: str) -> str:
    """Explicit text_link entities for every mela.team mention (UTF-16 offsets, as Telegram requires).
    A bare "mela.team" left to Telegram's auto-linking gave "Telegram can't open this link" (21.11, VARGEN)."""
    import json, re
    global _TG_LINK_RE
    if _TG_LINK_RE is None:
        _TG_LINK_RE = re.compile(r"(?<![\w@/.-])(?:https?://)?(?:www\.)?mela\.team(?:/[^\s)\]]*)?", re.I)
    ents = []
    for m in _TG_LINK_RE.finditer(text):
        raw = m.group(0).rstrip(".,!?;:")
        url = raw if raw.lower().startswith("http") else "https://" + raw
        off = len(text[:m.start()].encode("utf-16-le")) // 2
        ents.append({"type": "text_link", "offset": off, "length": len(raw.encode("utf-16-le")) // 2, "url": url})
    return json.dumps(ents)


class Telegram:
    """Telegram Bot API: posts to a channel where the bot is an admin."""

    name = "telegram"

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{_env('TELEGRAM_BOT_TOKEN')}/{method}"

    def publish(self, post: Post) -> dict:
        chat = post.option(self.name, "chat_id") or _env("TELEGRAM_CHAT_ID")
        text = post.text(self.name)
        imgs = post.images(self.name)
        video = post.video(self.name)
        if len(text) > 4096 or (imgs and len(text) > 1024):
            raise PostError("telegram: text too long (1024 with images, 4096 without)")
        if video:
            if video.stat().st_size > 50 * 1024 * 1024:
                raise PostError("telegram: video over 50 MB (Bot API upload limit)")
            short = len(text) <= 1024
            with video.open("rb") as f:
                body = _check(requests.post(self._api("sendVideo"),
                                            data={"chat_id": chat, "caption": text if short else "", "supports_streaming": "true",
                                                  "caption_entities": _tg_link_entities(text) if short else "[]"},
                                            files={"video": f}, timeout=300))
            if not short:  # long caption goes as a reply right under the video
                vid = (body.get("result") or {}).get("message_id")
                _check(requests.post(self._api("sendMessage"), data={"chat_id": chat, "text": text, "entities": _tg_link_entities(text), "reply_to_message_id": vid}, timeout=TIMEOUT))
        elif not imgs:
            body = _check(requests.post(self._api("sendMessage"), data={"chat_id": chat, "text": text, "entities": _tg_link_entities(text)}, timeout=TIMEOUT))
        elif len(imgs) == 1:
            with imgs[0].open("rb") as f:
                body = _check(requests.post(self._api("sendPhoto"), data={"chat_id": chat, "caption": text, "caption_entities": _tg_link_entities(text)},
                                            files={"photo": f}, timeout=TIMEOUT))
        else:
            if len(imgs) > 10:
                raise PostError("telegram: more than 10 images")
            import json
            files, media = {}, []
            for i, p in enumerate(imgs):
                key = f"f{i}"
                files[key] = p.open("rb")
                item = {"type": "photo", "media": f"attach://{key}"}
                if i == 0 and text:
                    item["caption"] = text
                    item["caption_entities"] = json.loads(_tg_link_entities(text))
                media.append(item)
            try:
                body = _check(requests.post(self._api("sendMediaGroup"), data={"chat_id": chat, "media": json.dumps(media)},
                                            files=files, timeout=TIMEOUT))
            finally:
                for f in files.values():
                    f.close()
        res = body.get("result")
        msg = res[0] if isinstance(res, list) else res
        out = {"id": msg.get("message_id") if msg else None}
        poll = post.option(self.name, "poll")
        if poll:
            import json
            question = str(poll.get("question", "")).strip()
            options = [str(o).strip() for o in poll.get("options", [])]
            if not question or len(question) > 300 or not 2 <= len(options) <= 10 or any(not o or len(o) > 100 for o in options):
                raise PostError("telegram poll: need a question (<=300 chars) and 2-10 options (<=100 chars each)")
            pbody = _check(requests.post(self._api("sendPoll"), data={
                "chat_id": chat, "question": question, "options": json.dumps(options),
                "is_anonymous": "true", "allows_multiple_answers": "false"}, timeout=TIMEOUT))
            out["poll_id"] = (pbody.get("result") or {}).get("message_id")
        return out


class ThreadsVasyl(Threads):
    """Second Threads account @isaiev_vasyl. Opt-in per post with a `threads_vasyl:` block
    (empty block = same text/images as `threads`). Secrets: THREADS_VASYL_ACCESS_TOKEN (+ optional THREADS_VASYL_USER_ID)."""

    name = "threads_vasyl"
    env_prefix = "THREADS_VASYL"
    username = os.environ.get("THREADS_VASYL_USERNAME", "isaiev_vasyl")

    def _verify_account(self) -> None:
        # a token generated for the wrong account must never post there (or be read as its history)
        me = _check(requests.get(f"{self.base}/me", params={"fields": "username", "access_token": self._token()}, timeout=TIMEOUT))
        if (me.get("username") or "").lower() != self.username.lower():
            raise NotConfigured(f"token belongs to @{me.get('username')}, expected @{self.username}")

    def recent_posts(self, hours: int = 48) -> list[dict]:
        self._verify_account()
        return super().recent_posts(hours)

    def publish(self, post: Post) -> dict:
        self._verify_account()
        return super().publish(post)

    def delete(self, media_id: str, permalink: str = "") -> dict:
        self._verify_account()
        return super().delete(media_id, permalink)
