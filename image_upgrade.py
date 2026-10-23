"""Explicit, reversible original-image replacement from an exact StashDB identity.

Never infers a full-body pose from dimensions. Credentials stay inside Stash's
configured scraper; public image downloads have no Stash cookies or API keys.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from PIL import Image

import entity_images
import stashdb_lookup
import maintenance
from performer_names import Resolver, read_json, write_json


ENDPOINT = "https://stashdb.org/graphql"
REVISION = 1
TTL = 1800
MAX_CANDIDATES = 24
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
COMMON = "id name image_path updated_at stash_ids { endpoint stash_id }"
FIELDS = {"performer": COMMON + " alias_list urls birthdate gender",
          "studio": COMMON + " aliases url urls"}


def validate(kind, identifier):
    if kind not in FIELDS or not isinstance(identifier, str) or not entity_images.numeric_id(identifier):
        raise ValueError("배우 또는 제작사 ID가 올바르지 않습니다.")


def remote_id(entity):
    ids = set()
    for item in entity.get("stash_ids") or []:
        if str(item.get("endpoint") or "").rstrip("/").lower() == ENDPOINT:
            value = str(item.get("stash_id") or "").lower()
            if not re.fullmatch(UUID, value):
                raise ValueError("StashDB 식별번호 형식이 올바르지 않습니다.")
            ids.add(value)
    if len(ids) > 1:
        raise ValueError("같은 항목에 서로 다른 StashDB 식별번호가 있어 사진 교체를 중지했습니다.")
    return next(iter(ids)) if ids else None


def fingerprint(entity):
    return hashlib.sha256(json.dumps(entity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def entity(client, kind, identifier):
    validate(kind, identifier)
    field = "find" + kind.capitalize()
    result = client.graphql(f"query OneClickHQEntity($id: ID!) {{ {field}(id: $id) {{ {FIELDS[kind]} }} }}", {"id": identifier}).get(field)
    if not isinstance(result, dict) or str(result.get("id")) != identifier:
        raise ValueError("배우·제작사 항목이 없어졌습니다. 다시 조회하세요.")
    return result


def catalog(client, kind):
    validate(kind, "1")
    field, collection = "find" + kind.capitalize() + "s", "performers" if kind == "performer" else "studios"
    people, expected, page = [], None, 1
    while True:
        result = client.graphql(f"query OneClickHQCatalog($filter: FindFilterType!) {{ {field}(filter: $filter) {{ count {collection} {{ {FIELDS[kind]} }} }} }}",
                                {"filter": {"page": page, "per_page": 100, "sort": "id", "direction": "ASC"}}).get(field) or {}
        count, batch = result.get("count"), result.get(collection)
        if not isinstance(count, int) or not isinstance(batch, list) or (expected is not None and count != expected):
            raise ValueError("목록이 조회 중 바뀌었습니다. 다시 조회하세요.")
        expected = count
        people.extend(batch)
        if len(people) >= count:
            break
        if not batch:
            raise ValueError("배우·제작사 목록을 끝까지 읽지 못했습니다.")
        page += 1
    if len(people) != expected or len({p["id"] for p in people}) != expected:
        raise ValueError("목록이 조회 중 바뀌었습니다. 다시 조회하세요.")
    items = []
    for p in people:
        try:
            linked = bool(remote_id(p))
            reason = "" if linked else ("제작사 이름·StashDB 연결 정리 작업 실행" if kind == "studio" else "설정 → 작업 → 영문 별명 StashDB 재스크래핑 실행")
        except ValueError as exc:
            linked, reason = False, str(exc)
        items.append({"id": p["id"], "name": p["name"], "linked": linked, "reason": reason})
    return {"ok": True, "kind": kind, "items": items, "linked": sum(p["linked"] for p in items), "total": len(items)}


def image_url(value):
    text = str(value or "")
    try:
        url = urllib.parse.urlsplit(text)
        valid = (url.scheme == "https" and url.hostname == "stashdb.org" and not url.username and not url.password
                 and url.port in (None, 443) and not url.query and not url.fragment
                 and re.fullmatch(r"/images/" + UUID, url.path, re.I))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("검증된 StashDB 원본 이미지 주소가 아닙니다.")
    return text


class ImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def inspect_bytes(body):
    if not body or len(body) > entity_images.MAX_IMAGE_BYTES:
        raise ValueError("이미지가 비어 있거나 5MB 제한을 넘었습니다.")
    with Image.open(io.BytesIO(body)) as picture:
        mime = entity_images.MIME_TYPES.get(picture.format)
        width, height = picture.size
        if not mime or min(width, height) < 20 or width * height > entity_images.MAX_PIXELS:
            raise ValueError("지원하지 않는 이미지 형식 또는 크기입니다.")
        picture.verify()
    with Image.open(io.BytesIO(body)) as picture:
        picture.load()
    return {"width": width, "height": height, "bytes": len(body), "mime": mime,
            "sha256": hashlib.sha256(body).hexdigest(), "data": f"data:{mime};base64," + base64.b64encode(body).decode("ascii")}


def download(url):
    request = urllib.request.Request(image_url(url), headers={"User-Agent": "JAVFanzaOneClick/1.9.0", "Accept": "image/*", "Accept-Encoding": "identity"})
    with urllib.request.build_opener(ImageRedirect()).open(request, timeout=25) as response:
        image_url(response.geturl())
        if int(response.headers.get("Content-Length") or 0) > entity_images.MAX_IMAGE_BYTES:
            raise ValueError("이미지가 5MB 제한을 넘었습니다.")
        return inspect_bytes(response.read(entity_images.MAX_IMAGE_BYTES + 1))


def stored_image(client, kind, item):
    if entity_images.missing_image(item):
        return None
    data = client.entity_image_data(kind, item["id"])
    match = re.fullmatch(r"data:image/(?:png|jpeg|gif|webp);base64,([A-Za-z0-9+/=]+)", data)
    if not match:
        raise ValueError("기존 사진을 보관할 수 없어 교체를 중지했습니다.")
    return inspect_bytes(base64.b64decode(match[1], validate=True))


def image_metrics(root, url, picture=None):
    """Bounded metadata-only cache. Never cache credentials or image payloads."""
    image_url(url)
    path = Path(root) / "image_metrics.json"
    now = time.time()
    cached = read_json(path)
    entries = cached.get("entries") or {}
    identifier = hashlib.sha256(url.encode()).hexdigest()
    if picture is None:
        item = entries.get(identifier, {})
        if (0 <= now - item.get("time", 0) < 30 * 86400 and
                all(isinstance(item.get(k), int) and item[k] > 0 for k in ("width", "height")) and
                re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", "")))):
            return item
        return None
    with maintenance.operation_lock(root, "image-metrics"):
        entries = (read_json(path).get("entries") or {})
        entries = {k: v for k, v in entries.items() if 0 <= now - v.get("time", 0) < 30 * 86400}
        entries[identifier] = {"time": now, **{k: picture[k] for k in ("width", "height", "sha256")}}
        entries = dict(sorted(entries.items(), key=lambda kv: kv[1]["time"], reverse=True)[:10000])
        write_json(path, {"entries": entries})
    return None


def source_record(client, kind, item, identifier=None):
    identifier = identifier or remote_id(item)
    if not identifier:
        raise ValueError("설정 → 작업에서 ‘제작사 이름·StashDB 연결 정리’를 실행하세요." if kind == "studio" else "설정 → 작업에서 ‘영문 별명으로 이름 변경 후 StashDB 재스크래핑’을 실행하세요.")
    endpoint = stashdb_lookup.configured_endpoint(client)
    result = stashdb_lookup.fetch(client, kind, identifier, endpoint, "OneClickHQSource")
    records = [r for r in result if str(r.get("remote_site_id") or "").lower() == identifier]
    if len(records) != 1:
        raise ValueError("StashDB에서 같은 식별번호의 항목 하나를 확인하지 못했습니다.")
    record = records[0]
    problem = stashdb_lookup.conflict(item, record, kind)
    if problem:
        raise ValueError(problem + ": 같은 항목인지 먼저 확인하세요.")
    return record


def save_ticket(root, stage, value):
    token = uuid.uuid4().hex
    write_json(root / "image_previews" / (token + ".json"), {**value, "revision": REVISION, "stage": stage, "time": time.time()})
    return token


def load_ticket(root, token, stage):
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
        raise ValueError("사진 미리보기 번호가 올바르지 않습니다.")
    value = read_json(root / "image_previews" / (token + ".json"))
    if (value.get("revision") != REVISION or value.get("stage") != stage or not isinstance(value.get("time"), (int, float))
            or not 0 <= time.time() - value["time"] <= TTL):
        raise ValueError("미리보기가 만료됐습니다. 원본 후보를 다시 조회하세요.")
    validate(value.get("kind"), value.get("id"))
    return value


def candidates(client, root, kind, identifier):
    item = entity(client, kind, identifier)
    source = source_record(client, kind, item)
    raw = (source.get("images") or []) if kind == "performer" else [source.get("image")]
    urls, rejected = [], 0
    for value in raw:
        if not value:
            continue
        try:
            url = image_url(value)
            if url not in urls:
                urls.append(url)
        except ValueError:
            rejected += 1
    ticket = save_ticket(root, "candidates", {"kind": kind, "id": identifier, "entity": item,
                                               "urls": urls[:MAX_CANDIDATES], "remote_id": remote_id(item)})
    return {"ok": True, "ticket": ticket, "kind": kind, "id": identifier, "name": item["name"], "source_name": source["name"], "remote_id": remote_id(item),
            "urls": urls[:MAX_CANDIDATES], "rejected": rejected,
            "truncated": len(urls) > MAX_CANDIDATES, "has_image": not entity_images.missing_image(item)}


def preview(client, root, args):
    plan = load_ticket(root, args.get("ticket"), "candidates")
    url = args.get("url")
    if url not in plan["urls"]:
        raise ValueError("조회한 원본 후보 중 하나를 선택하세요.")
    current = entity(client, plan["kind"], plan["id"])
    if fingerprint(current) != fingerprint(plan["entity"]):
        raise ValueError("후보 조회 후 항목이 바뀌었습니다. 다시 조회하세요.")
    new = download(url)
    old = stored_image(client, plan["kind"], current)
    token = save_ticket(root, "preview", {"kind": plan["kind"], "id": plan["id"], "entity": current,
                                          "url": url, "new_sha": new["sha256"], "old_sha": old["sha256"] if old else None})
    same = old is not None and old["sha256"] == new["sha256"]
    smaller = old is not None and (new["width"] * new["height"] < old["width"] * old["height"] or min(new["width"], new["height"]) < min(old["width"], old["height"]))
    return {"ok": True, "token": token, "name": current["name"], "old": old, "new": new, "same": same, "smaller": smaller}


def apply(client, root, args):
    if args.get("confirmed") is not True:
        raise ValueError("원본 사진을 확인하고 교체에 동의해야 합니다.")
    plan = load_ticket(root, args.get("token"), "preview")
    kind, identifier = plan["kind"], plan["id"]
    with entity_images.entity_lock(root, kind, identifier):
        current = entity(client, kind, identifier)
        if fingerprint(current) != fingerprint(plan["entity"]):
            raise ValueError("미리보기 이후 정보가 바뀌었습니다. 다시 조회하세요.")
        new = download(plan["url"])
        if new["sha256"] != plan["new_sha"]:
            raise ValueError("원본 사진이 미리보기 이후 변경됐습니다. 다시 확인하세요.")
        old = stored_image(client, kind, current)
        if (old["sha256"] if old else None) != plan["old_sha"]:
            raise ValueError("현재 사진이 다른 작업에서 바뀌었습니다. 교체하지 않았습니다.")
        if old and old["sha256"] == new["sha256"]:
            return {"ok": True, "status": "unchanged", "message": "이미 같은 원본 사진입니다."}
        if old and (new["width"] * new["height"] < old["width"] * old["height"] or min(new["width"], new["height"]) < min(old["width"], old["height"])) and args.get("allow_smaller") is not True:
            raise ValueError("현재보다 작은 사진입니다. 구도 선택을 위한 해상도 감소에 별도로 동의해야 합니다.")
        history_id = uuid.uuid4().hex
        path = root / "image_history" / (history_id + ".json")
        record = {"revision": REVISION, "kind": kind, "id": identifier, "before": current, "old": old,
                  "new_sha": new["sha256"], "source_url": plan["url"], "status": "prepared", "time": time.time()}
        write_json(path, record)  # Original preservation must succeed before any write to Stash.
        if fingerprint(entity(client, kind, identifier)) != fingerprint(current):
            raise ValueError("원본 보관 중 항목이 바뀌었습니다. 교체하지 않았습니다.")
        try:
            record["status"] = "update_requested"
            write_json(path, record)
            client.update_entity_image(kind, identifier, new["data"])
            after = entity(client, kind, identifier)
            saved = stored_image(client, kind, after)
            if not saved:
                raise ValueError("저장된 사진을 확인하지 못했습니다.")
            record.update(status="updated", after_sha=saved["sha256"], after_remote_id=remote_id(after))
            write_json(path, record)
        except Exception as exc:
            return {"ok": False, "status": "needs_check", "message": "사진 교체 결과를 확정하지 못했습니다. 자동 재시도하지 않습니다. 화면을 확인하세요.", "error": str(exc), "history": str(path)}
        return {"ok": True, "status": "updated", "kind": kind, "id": identifier, "name": current["name"],
                "width": saved["width"], "height": saved["height"], "history": str(path),
                "restore_token": history_id if old else None}


def restore(client, root, args):
    token = args.get("token")
    if args.get("confirmed") is not True or not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
        raise ValueError("원본 복원에 동의해야 합니다.")
    path = root / "image_history" / (token + ".json")
    record = read_json(path)
    if record.get("status") != "updated" or not record.get("old") or record.get("revision") not in (1, REVISION):
        raise ValueError("복원 가능한 원본 기록이 없습니다.")
    kind, identifier = record.get("kind"), record.get("id")
    validate(kind, identifier)
    with entity_images.entity_lock(root, kind, identifier):
        current = entity(client, kind, identifier)
        if remote_id(current) != record.get("after_remote_id", remote_id(record["before"])):
            raise ValueError("항목의 출처가 바뀌어 복원을 중지했습니다.")
        saved = stored_image(client, kind, current)
        if not saved or saved["sha256"] != record.get("after_sha"):
            raise ValueError("이후에 사진이 변경되어 덮어쓰지 않습니다.")
        try:
            client.update_entity_image(kind, identifier, record["old"]["data"])
            record["status"] = "restored"
            write_json(path, record)
        except Exception as exc:
            return {"ok": False, "status": "needs_check", "message": "복원 결과 확인이 필요합니다. 자동 재시도하지 않습니다.", "error": str(exc)}
        return {"ok": True, "status": "restored", "message": "교체 전 원본 사진을 복원했습니다."}


def name_preview(client, root, args):
    current = entity(client, "performer", args.get("id"))
    ids = entity_images.source_ids(current, "performer")
    if len(ids) != 1:
        raise ValueError("FANZA 배우 식별번호 하나가 확인되어야 이름 발음을 조회할 수 있습니다.")
    profile = Resolver(root).profile(next(iter(ids)))
    options = list(dict.fromkeys([profile["name_ko"], *[n for n in profile.get("name_aliases", []) if re.fullmatch(r"[가-힣 ]+", n)]]))
    token = save_ticket(root, "name", {"kind": "performer", "id": current["id"], "entity": current,
                                      "profile": profile, "options": options})
    return {"ok": True, "token": token, "before": current["name"], "original": profile["name_ja"],
            "reading": profile["name_reading"], "options": options, "aliases": profile.get("name_aliases", [])}


def name_apply(client, root, args):
    if args.get("confirmed") is not True:
        raise ValueError("배우의 공식 발음을 확인하고 이름 변경에 동의해야 합니다.")
    plan = load_ticket(root, args.get("token"), "name")
    selected = args.get("name")
    if selected not in plan["options"]:
        raise ValueError("확인된 발음 표기 중 하나를 선택하세요.")
    identifier = plan["id"]
    with entity_images.entity_lock(root, "performer", identifier):
        current = entity(client, "performer", identifier)
        if fingerprint(current) != fingerprint(plan["entity"]):
            raise ValueError("미리보기 이후 배우 정보가 바뀌었습니다. 다시 확인하세요.")
        aliases = list(dict.fromkeys([*(current.get("alias_list") or []), current["name"], plan["profile"]["name_ja"], *plan["profile"]["name_aliases"]]))
        aliases = [value for value in aliases if value and value != selected]
        path = root / "performer_names" / "manual_history" / (uuid.uuid4().hex + ".json")
        record = {"before": current, "name": selected, "aliases": aliases, "time": time.time(), "status": "prepared"}
        write_json(path, record)
        try:
            client.update_performer(identifier, selected, aliases)
            after = entity(client, "performer", identifier)
            if after["name"] != selected:
                raise ValueError("저장된 이름을 확인하지 못했습니다.")
            record["status"] = "updated"
            write_json(path, record)
            Resolver(root).mark_applied(identifier, plan["profile"]["remote_site_id"], selected, manual=True)
        except Exception as exc:
            return {"ok": False, "status": "needs_check", "message": "이름 변경 결과를 확인하세요. 자동 재시도하지 않습니다.", "error": str(exc)}
        return {"ok": True, "name": selected, "before": current["name"], "history": str(path)}
