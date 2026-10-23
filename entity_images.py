"""Fill missing Stash portraits/logos from identity-checked FANZA records.

This module never edits names, relationships, scene metadata or tags. Network
images are validated locally and sent to Stash as data URLs, without credentials.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import io
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from PIL import Image

import fanza_core


ENDPOINT = "https://api.video.dmm.co.jp/graphql"
IMAGE_HOSTS = {"awsimgsrc.dmm.co.jp", "pics.dmm.co.jp", "p.dmm.co.jp"}
SITE_HOSTS = {"www.dmm.co.jp", "video.dmm.co.jp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 16_000_000
KINDS = {"performer": "actress", "studio": "maker"}
MIME_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}


def name_key(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def numeric_id(value: Any) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"[0-9]{1,12}", text) else ""


def missing_image(entity: dict) -> bool:
    # Stash's URL builders append default=true to a placeholder image URL.
    # An omitted field is *unknown*, never proof that replacing an image is safe.
    if "image_path" not in entity:
        return False
    url = entity.get("image_path")
    if not url:
        return True
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(url)).query)
    return query.get("default") == ["true"]


def source_ids(entity: dict, kind: str) -> set[str]:
    article = KINDS[kind]
    ids = set()
    for value in [entity.get("url"), *(entity.get("urls") or [])]:
        if not value:
            continue
        try:
            url = urllib.parse.urlsplit(str(value))
            if url.scheme != "https" or url.hostname not in SITE_HOSTS or url.username or url.port not in (None, 443):
                continue
            match = re.search(rf"/article={article}/id=([0-9]{{1,12}})(?:/|$)", url.path)
            query = urllib.parse.parse_qs(url.query)
            value = match[1] if match else ""
            if url.hostname == "video.dmm.co.jp":
                if url.path.rstrip("/") == f"/av/{article}":
                    value = (query.get("id") or [""])[0]
                elif url.path.rstrip("/") == "/av/list":
                    value = (query.get(article) or [""])[0]
            if numeric_id(value):
                ids.add(value)
        except ValueError:
            continue
    return ids


def matches(entity: dict, source: dict, kind: str) -> bool:
    remote_id = numeric_id(source.get("id") or source.get("remote_site_id"))
    if not remote_id:
        return False
    known_ids = source_ids(entity, kind)
    if known_ids:
        return known_ids == {remote_id}
    aliases = "alias_list" if kind == "performer" else "aliases"
    local_names = {name_key(n) for n in [entity.get("name"), *(entity.get(aliases) or [])] if n}
    remote_names = {name_key(source.get(k)) for k in ("name", "name_ja", "name_ko") if source.get(k)}
    return bool(local_names & remote_names)


def image_url(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    try:
        url = urllib.parse.urlsplit(text)
        valid = (url.scheme == "https" and url.hostname in IMAGE_HOSTS
                 and not url.username and not url.password and url.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("FANZA 공식 이미지 서버 주소가 아닙니다.")
    if re.search(r"(?:now[_-]?printing|no[_-]?(?:image|photo)|spacer|placeholder)", url.path, re.I):
        raise ValueError("실제 사진 대신 기본 안내 이미지가 제공되었습니다.")
    return text


class ImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        image_url(newurl)  # Reject external/local redirects *before* following them.
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_image(url: str) -> str:
    url = image_url(url)
    request = urllib.request.Request(url, headers={
        "User-Agent": "JAVFanzaOneClick/1.8.0", "Accept": "image/*",
        "Referer": "https://video.dmm.co.jp/", "Accept-Encoding": "identity",
    })
    with urllib.request.build_opener(ImageRedirect()).open(request, timeout=15) as response:
        image_url(response.geturl())
        if int(response.headers.get("Content-Length") or 0) > MAX_IMAGE_BYTES:
            raise ValueError("이미지가 다운로드 크기 제한(5MB)을 넘었습니다.")
        body = response.read(MAX_IMAGE_BYTES + 1)
    if not body or len(body) > MAX_IMAGE_BYTES:
        raise ValueError("이미지가 비어 있거나 다운로드 크기 제한(5MB)을 넘었습니다.")
    # Decode, not just a Content-Type/header check: reject HTML and truncated files.
    with Image.open(io.BytesIO(body)) as decoded:
        mime = MIME_TYPES.get(decoded.format)
        width, height = decoded.size
        if not mime or min(width, height) < 20 or width * height > MAX_PIXELS:
            raise ValueError("지원하지 않는 이미지 형식 또는 크기입니다.")
        decoded.verify()
    with Image.open(io.BytesIO(body)) as decoded:
        decoded.load()
    return f"data:{mime};base64," + base64.b64encode(body).decode("ascii")


class FanzaProfiles:
    def __init__(self):
        self.profiles: dict[tuple[str, str], dict | None] = {}
        self.contents: dict[str, dict | None] = {}
        self.unavailable = False
        self.last_request = 0.0

    def query(self, query: str, identifier: str) -> dict:
        if self.unavailable:
            raise RuntimeError("FANZA 응답 오류로 이번 작업의 추가 조회를 중지했습니다. 나중에 다시 실행하세요.")
        time.sleep(max(0.0, 0.2 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        result = fanza_core.HTTP.post_json(ENDPOINT, {"query": query, "variables": {"id": identifier}})
        if not isinstance(result, dict) or result.get("errors") or not isinstance(result.get("data"), dict):
            self.unavailable = True
            raise RuntimeError("FANZA 사진·로고 정보를 가져오지 못했습니다. 나중에 다시 시도하세요.")
        return result["data"]

    def profile(self, kind: str, identifier: str) -> dict | None:
        if not numeric_id(identifier):
            return None
        key = (kind, identifier)
        if key not in self.profiles:
            field = KINDS[kind]
            value = self.query(f"query OneClickProfile($id: ID!) {{ {field}(id: $id) {{ id name imageUrl }} }}", identifier).get(field)
            if value is not None and (not isinstance(value, dict) or str(value.get("id")) != identifier):
                raise ValueError("FANZA 식별번호가 요청한 배우·제작사와 다릅니다.")
            self.profiles[key] = value
        return self.profiles[key]

    def content(self, cid: str) -> dict | None:
        if cid not in self.contents:
            query = """query OneClickImages($id: ID!) { ppvContent(id: $id) {
              id maker { id name imageUrl } actresses { id name imageUrl }
            } }"""
            value = self.query(query, cid).get("ppvContent")
            if value is not None and (not isinstance(value, dict)
                                     or fanza_core.canonical_fanza_id(str(value.get("id") or "")) != fanza_core.canonical_fanza_id(cid)):
                raise ValueError("FANZA 작품 식별번호가 요청한 작품과 다릅니다.")
            self.contents[cid] = value
        return self.contents[cid]

    def scene_sources(self, scene: dict, code: str | None, metadata: dict | None = None) -> dict:
        urls = [*((metadata or {}).get("urls") or []), *(scene.get("urls") or [])]
        candidates = fanza_core.make_cid_candidates(code) if code else []
        allowed = {fanza_core.canonical_fanza_id(c) for c in candidates}
        if code:
            allowed.add(fanza_core.canonical_fanza_id(code))
        found = []
        for value in urls:
            try:
                url = urllib.parse.urlsplit(str(value))
                if url.scheme != "https" or url.hostname not in SITE_HOSTS or url.username or url.port not in (None, 443):
                    continue
                cid = ""
                if url.hostname == "video.dmm.co.jp" and url.path.rstrip("/") == "/av/content":
                    cid = (urllib.parse.parse_qs(url.query).get("id") or [""])[0]
                elif url.hostname == "www.dmm.co.jp":
                    match = re.search(r"/detail/=/cid=([A-Za-z0-9_]+)(?:/|$)", url.path)
                    cid = match[1] if match else ""
                if re.fullmatch(r"[A-Za-z0-9_]{2,60}", cid) and (not code or fanza_core.canonical_fanza_id(cid) in allowed or fanza_core.matches_discovered_id(code, cid)):
                    if cid not in found:
                        found.append(cid)
            except ValueError:
                continue
        if not code and len({fanza_core.canonical_fanza_id(c) for c in found}) > 1:
            return {}
        # No fuzzy search and no title translation in an image-only operation.
        for cid in list(dict.fromkeys(found + candidates))[:8]:
            value = self.content(cid)
            if value:
                return value
        return {}


@contextmanager
def entity_lock(root: Path, kind: str, identifier: str):
    if kind not in KINDS or not numeric_id(identifier):
        raise ValueError("잘못된 Stash 배우·제작사 ID입니다.")
    folder = root / "image_locks"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{kind}-{identifier}.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("다른 이미지 작업이 처리 중입니다. 완료 후 다시 시도하세요.") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Enricher:
    def __init__(self, client, root: Path, provider=None, downloader=None):
        self.client = client
        self.root = root
        self.provider = provider or FanzaProfiles()
        self.downloader = downloader or download_image
        self.results: dict[tuple[str, str], dict] = {}
        self.downloads: dict[str, str] = {}
        self.download_errors: dict[str, str] = {}

    def stashdb_fill(self, kind, entity):
        import image_upgrade as hq
        if not hq.remote_id(entity):
            return False
        current = hq.entity(self.client, kind, str(entity["id"]))
        source = hq.source_record(self.client, kind, current)
        urls = (source.get("images") or []) if kind == "performer" else [source.get("image")]
        last_error = None
        for url in list(dict.fromkeys(u for u in urls if u))[:hq.MAX_CANDIDATES]:
            try:
                picture = hq.download(url)
                with entity_lock(self.root, kind, str(entity["id"])):
                    latest = hq.entity(self.client, kind, str(entity["id"]))
                    if not missing_image(latest):
                        return "existing"
                    if hq.fingerprint(latest) != hq.fingerprint(current):
                        raise ValueError("조회 중 항목이 변경되었습니다. 다시 실행하세요.")
                    self.client.update_entity_image(kind, str(entity["id"]), picture["data"])
                return "filled"
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        return False

    def fill(self, kind: str, entity: dict, sources: list[dict] | None = None) -> dict:
        key = (kind, str(entity.get("id") or ""))
        previous = self.results.get(key)
        if previous and previous["status"] in {"filled", "existing", "no_image"}:
            return previous
        result = {"kind": kind, "id": key[1], "name": entity.get("name") or "", "status": "unmatched"}
        try:
            if "image_path" not in entity:
                result["status"] = "unknown"
            elif not missing_image(entity):
                result["status"] = "existing"
            else:
                try:
                    stash_status = self.stashdb_fill(kind, entity)
                    if stash_status:
                        result["status"] = stash_status
                        self.results[key] = result
                        return result
                except Exception as exc:
                    result["stashdb_warning"] = str(exc)
                known = source_ids(entity, kind)
                candidates = [s for s in sources or [] if isinstance(s, dict) and matches(entity, s, kind)]
                unique = {str(s.get("id") or s.get("remote_site_id")): s for s in candidates}
                source = None
                if len(known) == 1:
                    source = self.provider.profile(kind, next(iter(known)))
                elif not known and len(unique) == 1:
                    source = next(iter(unique.values()))
                    if "imageUrl" not in source:
                        source = self.provider.profile(kind, str(source.get("id") or source.get("remote_site_id")))
                if source and matches(entity, source, kind):
                    if not source.get("imageUrl"):
                        result["status"] = "no_image"
                    else:
                        url = image_url(source["imageUrl"])
                        with entity_lock(self.root, kind, key[1]):
                            if url in self.download_errors:
                                raise RuntimeError(self.download_errors[url])
                            if url not in self.downloads:
                                try:
                                    self.downloads[url] = self.downloader(url)
                                except Exception as exc:
                                    self.download_errors[url] = str(exc)
                                    raise
                                if len(self.downloads) > 16:
                                    self.downloads.pop(next(iter(self.downloads)))
                            # Re-read *after* downloading, so another hook/manual edit wins.
                            latest = self.client.find_image_entity(kind, key[1])
                            if not latest or "image_path" not in latest:
                                result["status"] = "unknown"
                            elif not missing_image(latest):
                                result["status"] = "existing"
                            elif matches(latest, source, kind):
                                self.client.update_entity_image(kind, key[1], self.downloads[url])
                                result["status"] = "filled"
        except Exception as exc:
            result.update(status="failed", error=str(exc))
        self.results[key] = result
        return result

    def scene(self, scene: dict, code: str | None, metadata: dict | None = None) -> dict:
        previously_filled = {k for k, r in self.results.items() if r["status"] == "filled"}
        entities = [("performer", e) for e in scene.get("performers") or []]
        if scene.get("studio"):
            entities.append(("studio", scene["studio"]))
        unresolved = []
        for kind, entity in entities:
            sources = ((metadata or {}).get("performers") or []) if kind == "performer" else [(metadata or {}).get("studio") or {}]
            result = self.fill(kind, entity, sources)
            if result["status"] == "unmatched" and missing_image(entity):
                unresolved.append((kind, entity))
        if unresolved:
            try:
                data = self.provider.scene_sources(scene, code, metadata)
                for kind, entity in unresolved:
                    sources = (data.get("actresses") or []) if kind == "performer" else [data.get("maker") or {}]
                    self.fill(kind, entity, sources)
            except Exception as exc:
                for kind, entity in unresolved:
                    self.results[(kind, str(entity["id"]))].update(status="failed", error=str(exc))
        return self.summary([(kind, str(e["id"])) for kind, e in entities], previously_filled)

    def summary(self, keys=None, previously_filled=None) -> dict:
        keys = self.results if keys is None else keys
        items = [({**self.results[k], "status": "existing"} if k in (previously_filled or set()) else self.results[k])
                 for k in dict.fromkeys(keys) if k in self.results]
        counts = Counter(item["status"] for item in items)
        return {
            "ok": not counts["failed"], "checked": len(items), "filled": counts["filled"],
            "portraits": sum(i["status"] == "filled" and i["kind"] == "performer" for i in items),
            "logos": sum(i["status"] == "filled" and i["kind"] == "studio" for i in items),
            "existing": counts["existing"], "unmatched": counts["unmatched"],
            "no_image": counts["no_image"], "unknown": counts["unknown"], "failed": counts["failed"],
            "errors": [i for i in items if i["status"] == "failed"][:20],
            "source_warnings": [{"kind":i["kind"], "id":i["id"], "warning":i["stashdb_warning"]} for i in items if i.get("stashdb_warning")][:20],
        }
