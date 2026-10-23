"""Promote existing English aliases and rescrape those performers from StashDB.

No FANZA name generation, scene/tag edits, actor creation or implicit merging.
Every update is backed up, preserves former names, and uses an exact source UUID.
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import entity_images
import image_upgrade as hq
import performer_duplicates as duplicates
import stashdb_lookup as lookup
from performer_names import write_json


def person(client, identifier):
    if not isinstance(identifier, str) or not entity_images.numeric_id(identifier):
        raise ValueError("배우 ID가 올바르지 않습니다.")
    result = client.graphql(f"query OneClickAliasPerson($id: ID!) {{ findPerformer(id: $id) {{ {duplicates.FIELDS} }} }}", {"id": identifier}).get("findPerformer")
    if not isinstance(result, dict) or str(result.get("id")) != identifier:
        raise ValueError("배우 항목을 확인하지 못했습니다.")
    return result


def people(client, args):
    if args.get("id") is not None:
        return [person(client, args["id"])]
    rows, page, expected = [], 1, None
    while True:
        result = client.graphql(f"query OneClickAliasPeople($filter: FindFilterType!) {{ findPerformers(filter: $filter) {{ count performers {{ {duplicates.FIELDS} }} }} }}",
                                {"filter": {"page": page, "per_page": 100, "sort": "id", "direction": "ASC"}}).get("findPerformers") or {}
        count, batch = result.get("count"), result.get("performers")
        if not isinstance(count, int) or not isinstance(batch, list) or (expected is not None and expected != count):
            raise ValueError("배우 목록이 변경됐습니다. 다시 시작하세요.")
        expected = count
        rows.extend(batch)
        if len(rows) >= count:
            break
        if not batch:
            raise ValueError("배우 목록을 끝까지 읽지 못했습니다.")
        page += 1
    if len(rows) != expected or len({p["id"] for p in rows}) != expected:
        raise ValueError("배우 목록이 조회 중 변경됐습니다.")
    return rows


def scan(client, args):
    """Fast local inventory only; no remote search or mutation."""
    lookup.configured_endpoint(client)
    rows = people(client, args)
    items = [{"id": p["id"], "name": p["name"], "english_aliases": lookup.english_aliases(p)} for p in rows]
    return {"ok": True, "total": len(items), "eligible": sum(bool(p["english_aliases"]) for p in items), "items": items}


def safe_urls(values):
    result = []
    for value in values:
        try:
            url = urlsplit(value)
            if url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password:
                result.append(value)
        except (ValueError, TypeError):
            pass
    return result


def metadata_values(before, resolved):
    source, name = resolved.get("source"), resolved["name"]
    if not name:
        raise ValueError("승격할 영문 별명을 확인하지 못했습니다.")
    values = {"id": before["id"]}
    if name != before["name"]:
        values["name"] = name
    aliases = duplicates.unique_strings([*(before.get("alias_list") or []), before["name"], *(lookup.source_names(source) if source else [])], name)
    if aliases != (before.get("alias_list") or []):
        values["alias_list"] = aliases
    if not source:
        return values
    urls = list(dict.fromkeys([*(before.get("urls") or []), *safe_urls(source.get("urls") or [])]))
    if urls != (before.get("urls") or []):
        values["urls"] = urls
    if not hq.remote_id(before):
        values["stash_ids"] = [*(before.get("stash_ids") or []), {"endpoint": lookup.ENDPOINT, "stash_id": resolved["remote_id"]}]
    for key in ("disambiguation", "gender", "birthdate", "death_date", "ethnicity", "country", "eye_color", "hair_color",
                "measurements", "fake_tits", "career_start", "career_end", "tattoos", "piercings", "details"):
        value = source.get(key)
        if before.get(key) in (None, "") and isinstance(value, str) and value.strip():
            if key == "gender" and value not in {"MALE", "FEMALE", "TRANSGENDER_MALE", "TRANSGENDER_FEMALE", "INTERSEX", "NON_BINARY"}:
                continue
            values[key] = value
    for target, key, lower, upper in (("height_cm", "height", 50, 250), ("weight", "weight", 20, 350)):
        value = str(source.get(key) or "")
        if before.get(target) is None and re.fullmatch(r"\d{1,3}", value) and lower <= int(value) <= upper:
            values[target] = int(value)
    return values


def best_image(client, before, source, policy, root=None):
    if not entity_images.missing_image(before) and policy == "missing":
        return None, None, []
    old = hq.stored_image(client, "performer", before)
    best, errors = None, []
    urls = list(dict.fromkeys(source.get("images") or []))[:hq.MAX_CANDIDATES]
    for url in urls:
        try:
            cached = hq.image_metrics(root, url) if root else None
            if old and cached and (cached["sha256"] == old["sha256"] or cached["width"] * cached["height"] <= old["width"] * old["height"]
                                  or min(cached["width"], cached["height"]) < min(old["width"], old["height"])):
                continue
            new = hq.download(url)
            if root:
                try:
                    hq.image_metrics(root, url, new)
                except (OSError, RuntimeError):
                    pass  # Optional cache must not block an otherwise valid image.
            if old and (new["sha256"] == old["sha256"] or new["width"] * new["height"] <= old["width"] * old["height"]
                        or min(new["width"], new["height"]) < min(old["width"], old["height"])):
                continue
            if not best or (new["width"] * new["height"], min(new["width"], new["height"])) > (best["width"] * best["height"], min(best["width"], best["height"])):
                best = new
        except Exception as exc:
            errors.append(str(exc))
    return best, old, errors


def apply_one(client, root, before, resolved, policy, batch_id, before_write=None):
    identifier = before["id"]
    with entity_images.entity_lock(root, "performer", identifier):
        current = person(client, identifier)
        if hq.fingerprint(current) != hq.fingerprint(before):
            return {"status": "changed", "reason": "작업 중 변경된 배우: 이번에는 건너뜀"}
        values = metadata_values(current, resolved)
        image, old, warnings = None, None, []
        try:
            if resolved.get("source"):
                image, old, warnings = best_image(client, current, resolved["source"], policy, root)
            if image:
                values["image"] = image["data"]
        except Exception as exc:
            warnings.append("사진은 보존: " + str(exc))
        if len(values) == 1:
            return {"status": "unchanged", "name": current["name"], "warnings": warnings}
        if before_write:
            before_write()
        path = root / "stashdb_rescrape" / batch_id / (identifier + ".json")
        record = {"version": 1, "time": time.time(), "status": "prepared", "before": current, "old_image": old,
                  "source_id": resolved.get("remote_id"), "values": {k: v for k, v in values.items() if k != "image"},
                  "new_image_sha": image["sha256"] if image else None}
        write_json(path, record)
        if hq.fingerprint(person(client, identifier)) != hq.fingerprint(current):
            return {"status": "changed", "reason": "저장 전 정보 변경: 이번에는 건너뜀"}
        if image:
            latest = hq.stored_image(client, "performer", current)
            if (latest["sha256"] if latest else None) != (old["sha256"] if old else None):
                return {"status": "changed", "reason": "다른 작업에서 사진 변경: 이번에는 건너뜀"}
        record["status"] = "update_requested"
        write_json(path, record)
        try:
            response = client.graphql("mutation OneClickAliasUpdate($input: PerformerUpdateInput!) { performerUpdate(input: $input) { id } }", {"input": values})
            if str((response.get("performerUpdate") or {}).get("id")) != identifier:
                raise ValueError("Stash 저장 응답을 확인하지 못했습니다.")
            after = person(client, identifier)
            if after["name"] != resolved["name"] or (resolved.get("source") and hq.remote_id(after) != resolved["remote_id"]):
                raise ValueError("저장된 이름·StashDB 연결을 확인하지 못했습니다.")
            if current["name"].casefold() != after["name"].casefold() and current["name"].casefold() not in {a.casefold() for a in after.get("alias_list") or []}:
                raise ValueError("기존 이름의 별명 보관을 확인하지 못했습니다.")
            saved_image = hq.stored_image(client, "performer", after) if image else None
            if image and not saved_image:
                raise ValueError("저장된 사진을 확인하지 못했습니다.")
            record.update(status="updated", after=after, after_image_sha=saved_image["sha256"] if saved_image else None)
            write_json(path, record)
        except Exception as exc:
            record.update(status="needs_check", error=str(exc))
            try:
                write_json(path, record)
            except OSError:
                pass
            return {"status": "needs_check", "reason": str(exc), "history": str(path)}
        return {"status": "updated" if resolved.get("source") else "renamed_only", "name": after["name"], "renamed": "name" in values, "photo": bool(image),
                "fields": [k for k in values if k != "id"], "warnings": warnings, "history": str(path)}


def run(client, root, args, log=lambda text: None):
    if args.get("confirmed") is not True:
        raise ValueError("영문 이름 변경과 StashDB 재스크래핑에 동의해야 합니다.")
    policy = args.get("photo_policy", "missing")
    if policy not in {"missing", "upgrade"}:
        raise ValueError("사진 처리 방식이 올바르지 않습니다.")
    with entity_images.entity_lock(root, "performer", "0"):
        endpoint = lookup.configured_endpoint(client)
        rows = people(client, args)
        batch_id = uuid.uuid4().hex
        path = root / "stashdb_rescrape" / batch_id / "result.json"
        report = {"ok": True, "total": len(rows), "checked": 0, "renamed": 0, "scraped": 0, "photos": 0, "unchanged": 0,
                  "skipped": 0, "failed": 0, "needs_check": 0, "backup_status": "not_requested", "photo_policy": policy, "results": [], "history": str(path)}
        write_json(path, report)
        backed_up = False
        def ensure_backup():
            nonlocal backed_up
            if backed_up:
                return
            receipt = client.graphql("mutation OneClickAliasBackup($input: BackupDatabaseInput!) { backupDatabase(input: $input) }", {"input": {"download": False, "includeBlobs": True}}, timeout=300)
            if "backupDatabase" not in receipt or (receipt["backupDatabase"] is not None and (not isinstance(receipt["backupDatabase"], str) or not receipt["backupDatabase"].strip())):
                raise ValueError("Stash 백업 완료를 확인하지 못했습니다. 배우를 변경하지 않았습니다.")
            backed_up = True
            report.update(backup_status="completed", backup=receipt["backupDatabase"])
            write_json(path, report)
        for index, before in enumerate(rows, 1):
            result = {"id": before["id"], "before": before["name"]}
            resolved = None
            try:
                log(f"영문 별명 → StashDB: {index}/{len(rows)} · #{before['id']} 검색")
                resolved = lookup.resolve(client, before, endpoint)
                if resolved["status"] in {"unmatched", "ambiguous"}:
                    # The requested name promotion still works when StashDB has
                    # no unique record. Metadata/source IDs remain unchanged.
                    names = lookup.english_aliases(before)
                    result["scrape_status"] = resolved["status"]
                    result["reason"] = resolved.get("reason")
                    current_english = lookup.english(before["name"])
                    selected = current_english or next((n for n in names if len(n.split()) == 2), names[0])
                    resolved = {"status": "matched", "name": selected, "source": None}
                if resolved["status"] != "matched":
                    result.update({k: v for k, v in resolved.items() if k != "source"})
                else:
                    if not resolved.get("source") and len(metadata_values(before, resolved)) == 1:
                        result.update(status="unchanged", name=before["name"])
                        resolved = None
                    if resolved is None:
                        pass
                    if resolved is not None:
                        result.update(apply_one(client, root, before, resolved, policy, batch_id, ensure_backup))
            except Exception as exc:
                result.update(status="failed", reason=str(exc))
                # A backup or disk failure must not restart a bulk mutation loop.
                if not backed_up and resolved and resolved.get("status") == "matched":
                    report["stop_reason"] = str(exc)
            report["checked"] += 1
            report["results"].append(result)
            status = result["status"]
            if status == "updated":
                report["scraped"] += 1
                report["renamed"] += int(result.get("renamed", False))
                report["photos"] += int(result.get("photo", False))
            elif status == "renamed_only":
                report["renamed"] += int(result.get("renamed", False))
                report["skipped"] += 1
            elif status in {"failed", "needs_check", "unchanged"}:
                report[status] += 1
            else:
                report["skipped"] += 1
            report["ok"] = not (report["failed"] or report["needs_check"])
            write_json(path, report)
            log(f"영문 별명 → StashDB: #{before['id']} {status}" + (f" · {result.get('name')}" if result.get("name") else f" · {result.get('reason', '')}"))
            if status == "needs_check" or report.get("stop_reason"):
                report["stop_reason"] = report.get("stop_reason") or "저장 응답이 불확실해 중지했습니다. 자동 재시도하지 않습니다."
                break
        write_json(path, report)
        log(f"영문 별명 → StashDB 결과: 이름 {report['renamed']}명, 정보 {report['scraped']}명, 사진 {report['photos']}개, 보류 {report['skipped']}명, 실패 {report['failed']}명. 기록: {path}")
        return report
