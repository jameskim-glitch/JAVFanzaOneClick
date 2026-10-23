"""Conservative duplicate discovery and explicit, backed-up performer merges.

Discovery is local and read-only. Names are evidence for review, never proof.
Each merge handles exactly two records: weak matches cannot form transitive groups.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
import urllib.parse
import uuid

import entity_images
from performer_names import write_json


ALGORITHM = "performer-duplicates-v1"
PAGE_SIZE = 100
MAX_CANDIDATES = 500
MAX_NAME_BUCKET = 20
FIELDS = """id name disambiguation alias_list urls stash_ids { endpoint stash_id }
  gender birthdate death_date ethnicity country eye_color hair_color height_cm weight
  measurements fake_tits penis_length circumcised career_start career_end tattoos piercings
  favorite rating100 ignore_auto_tag details custom_fields tags { id name }
  image_path scene_count image_count gallery_count updated_at"""
SCALAR_FIELDS = (
    "disambiguation", "gender", "birthdate", "death_date", "ethnicity", "country",
    "eye_color", "hair_color", "height_cm", "weight", "measurements", "fake_tits",
    "penis_length", "circumcised", "career_start", "career_end", "tattoos", "piercings", "rating100",
)


def normal(value) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def name_forms(person: dict) -> dict[str, str]:
    forms = {}
    for value in [person.get("name"), *(person.get("alias_list") or [])]:
        text = normal(value)
        if not text:
            continue
        variants = [text]
        parenthetical = re.fullmatch(r"([^()]+)\(([^()]+)\)", text)
        if parenthetical:
            variants += [parenthetical[1], *re.split(r"[,、]", parenthetical[2])]
        for variant in variants:
            compact = re.sub(r"\s+", "", variant)
            # Single short stage names produce many unrelated people.
            if len(compact) < 3 or compact in {"unknown", "unnamed", "미상", "不明"}:
                continue
            forms["name:" + compact] = variant.strip()
            # Explicit full Roman names may be stored in either order. Review only.
            if re.fullmatch(r"[a-z]{2,}(?:[ -][a-z]{2,}){1,3}", variant):
                forms["roman:" + "|".join(sorted(re.split(r"[ -]+", variant)))] = variant.strip()
    return forms


def identities(person: dict) -> dict[str, set[str]]:
    result = {"FANZA": entity_images.source_ids(person, "performer")}
    for item in person.get("stash_ids") or []:
        try:
            endpoint = urllib.parse.urlsplit(str(item.get("endpoint") or ""))
            identifier = str(item.get("stash_id") or "").strip().lower()
            if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                    or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                    or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", identifier)):
                continue
            key = urllib.parse.urlunsplit((endpoint.scheme.lower(), endpoint.netloc.lower(),
                                          endpoint.path.rstrip("/"), "", ""))
            result.setdefault(key, set()).add(identifier)
        except (ValueError, AttributeError):
            continue
    return {key: value for key, value in result.items() if value}


def classify(left: dict, right: dict) -> dict | None:
    a, b = identities(left), identities(right)
    shared = [(key, sorted(a[key] & b[key])) for key in sorted(a.keys() & b.keys()) if a[key] & b[key]]
    names_a, names_b = name_forms(left), name_forms(right)
    shared_names = sorted(names_a.keys() & names_b.keys())
    if not shared and not shared_names:
        return None
    evidence = [f"{key} 식별번호 일치: {', '.join(ids)}" for key, ids in shared]
    evidence += [f"{'영문 성·이름 순서' if key.startswith('roman:') else '이름·별칭'} 일치: {names_a[key]}"
                 for key in shared_names[:5]]
    conflicts = []
    for key in sorted(a.keys() | b.keys()):
        if len(a.get(key, set()) | b.get(key, set())) > 1:
            conflicts.append(f"{key}에 서로 다른 식별번호가 있습니다.")
    for field, label in (("birthdate", "생년월일"), ("gender", "성별")):
        first, second = str(left.get(field) or ""), str(right.get(field) or "")
        if first and second and first != second:
            if field == "birthdate" and (first.startswith(second) or second.startswith(first)):
                continue
            conflicts.append(f"{label} 불일치: {first} / {second}")
    return {"confidence": "blocked" if conflicts else "identifier" if shared else "review",
            "evidence": evidence, "conflicts": conflicts}


def has_image(person: dict) -> bool:
    return bool(person.get("image_path")) and not entity_images.missing_image(person)


def summary(person: dict) -> dict:
    keys = ("id", "name", "alias_list", "birthdate", "gender", "scene_count", "image_count", "gallery_count",
            "favorite", "rating100", "image_path", "disambiguation")
    return {**{key: person.get(key) for key in keys},
            "identities": {key: sorted(value) for key, value in identities(person).items()}}


def recommendation(people: list[dict]) -> str:
    def score(p):
        richness = sum(p.get(key) not in (None, "", [], {}) for key in SCALAR_FIELDS)
        richness += 3 * has_image(p) + 3 * bool(p.get("stash_ids")) + 3 * bool(p.get("details"))
        return (-richness, -int(p.get("scene_count") or 0), int(p["id"]))
    return str(min(people, key=score)["id"])


def discover(people: list[dict]) -> dict:
    by_id = {str(p["id"]): p for p in people}
    if len(by_id) != len(people):
        raise ValueError("배우 목록이 조회 중 변경됐습니다. 다시 검사하세요.")
    buckets = defaultdict(set)
    for identifier, person in by_id.items():
        for scope, values in identities(person).items():
            for value in values:
                buckets[("identity", scope, value)].add(identifier)
        for key in name_forms(person):
            buckets[("name", key)].add(identifier)
    pairs = set()
    common = 0
    # Bound memory for malformed libraries containing thousands of identical aliases/IDs.
    for key, identifiers in sorted(buckets.items()):
        if len(identifiers) > MAX_NAME_BUCKET:
            common += 1
            continue
        identifiers = sorted(identifiers, key=int)
        for index, first in enumerate(identifiers):
            pairs.update((first, second) for second in identifiers[index + 1:])
    candidates = []
    for first, second in sorted(pairs, key=lambda pair: tuple(map(int, pair))):
        people_pair = [by_id[first], by_id[second]]
        match = classify(*people_pair)
        if match:
            candidates.append({"ids": [first, second], **match,
                               "people": [summary(p) for p in people_pair],
                               "recommended_id": recommendation(people_pair)})
    order = {"identifier": 0, "review": 1, "blocked": 2}
    candidates.sort(key=lambda c: (order[c["confidence"]], tuple(map(int, c["ids"]))))
    return {"ok": True, "algorithm": ALGORITHM, "checked": len(people),
            "identifier_matches": sum(c["confidence"] == "identifier" for c in candidates),
            "review_matches": sum(c["confidence"] == "review" for c in candidates),
            "blocked_matches": sum(c["confidence"] == "blocked" for c in candidates),
            "total_candidates": len(candidates), "truncated": len(candidates) > MAX_CANDIDATES,
            "ignored_common_keys": common, "candidates": candidates[:MAX_CANDIDATES]}


def scan(client) -> dict:
    people, page, expected = [], 1, None
    while True:
        data = client.graphql(f"""query OneClickDuplicateActors($filter: FindFilterType!) {{
          findPerformers(filter: $filter) {{ count performers {{ {FIELDS} }} }}
        }}""", {"filter": {"page": page, "per_page": PAGE_SIZE, "sort": "id", "direction": "ASC"}})
        result = data.get("findPerformers") or {}
        count, batch = result.get("count"), result.get("performers")
        if not isinstance(count, int) or not isinstance(batch, list):
            raise ValueError("Stash 배우 목록을 확인하지 못했습니다.")
        if expected is None:
            expected = count
        if expected != count:
            raise ValueError("검사 중 배우 수가 바뀌었습니다. 다른 작업이 끝난 뒤 다시 검사하세요.")
        people.extend(batch)
        if len(people) >= count:
            break
        if not batch:
            raise ValueError("배우 목록이 중간에 끊겼습니다. 다시 검사하세요.")
        page += 1
    if len(people) != expected:
        raise ValueError("배우 목록의 개수가 맞지 않습니다. 다시 검사하세요.")
    return discover(people)


def pair_ids(args: dict) -> tuple[list[str], str]:
    ids = args.get("ids")
    destination = str(args.get("destination") or "")
    if (not isinstance(ids, list) or len(ids) != 2 or any(not isinstance(i, str) or not entity_images.numeric_id(i) for i in ids)
            or len(set(ids)) != 2 or destination not in ids):
        raise ValueError("배우 두 명과 남길 항목 하나를 선택하세요.")
    return sorted(ids, key=int), destination


def load_pair(client, ids: list[str]) -> list[dict]:
    people = []
    for identifier in ids:
        data = client.graphql(f"query OneClickDuplicateActor($id: ID!) {{ findPerformer(id: $id) {{ {FIELDS} }} }}",
                              {"id": identifier})
        person = data.get("findPerformer")
        if not isinstance(person, dict) or str(person.get("id")) != identifier:
            raise ValueError("배우 항목이 없어졌거나 변경됐습니다. 목록을 다시 검사하세요.")
        people.append(person)
    return people


def snapshot_token(people: list[dict], destination: str) -> str:
    raw = json.dumps([ALGORITHM, destination, sorted(people, key=lambda p: int(p["id"]))],
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def unique_strings(values, excluded="") -> list[str]:
    result, seen = [], {normal(excluded)}
    for value in values:
        if value and normal(value) not in seen:
            result.append(str(value))
            seen.add(normal(value))
    return result


def plan(people: list[dict], destination: str) -> dict:
    match = classify(*people)
    if not match:
        raise ValueError("일치하는 식별번호나 이름·별칭이 없어 중복 후보로 볼 수 없습니다.")
    if match["confidence"] == "blocked":
        raise ValueError("병합 차단: " + " / ".join(match["conflicts"]))
    dest = next(p for p in people if str(p["id"]) == destination)
    source = next(p for p in people if str(p["id"]) != destination)
    values = {"id": destination,
              "alias_list": unique_strings([*(dest.get("alias_list") or []), source["name"], *(source.get("alias_list") or [])], dest["name"]),
              "urls": list(dict.fromkeys([*(dest.get("urls") or []), *(source.get("urls") or [])])),
              "favorite": bool(dest.get("favorite") or source.get("favorite")),
              "ignore_auto_tag": bool(dest.get("ignore_auto_tag") or source.get("ignore_auto_tag"))}
    stash_ids = {}
    for p in (dest, source):
        for item in p.get("stash_ids") or []:
            stash_ids[(item["endpoint"], item["stash_id"])] = {key: item[key] for key in ("endpoint", "stash_id")}
    values["stash_ids"] = list(stash_ids.values())
    conflicts = []
    filled = []
    for key in SCALAR_FIELDS:
        current, incoming = dest.get(key), source.get(key)
        if current in (None, "") and incoming not in (None, ""):
            values[key] = incoming
            filled.append(key)
        elif current not in (None, "") and incoming not in (None, "") and current != incoming:
            conflicts.append({"field": key, "kept": current, "other": incoming})
    details = str(dest.get("details") or "")
    extra = str(source.get("details") or "")
    if extra and extra != details:
        values["details"] = (details + f"\n\n[병합된 배우 #{source['id']}: {source['name']}]\n" + extra).strip() if details else extra
    target_fields, source_fields = dest.get("custom_fields") or {}, source.get("custom_fields") or {}
    for key in sorted(target_fields.keys() & source_fields.keys()):
        if target_fields[key] != source_fields[key]:
            conflicts.append({"field": "custom_fields." + key, "kept": target_fields[key], "other": source_fields[key]})
    if source_fields:
        values["custom_fields"] = {"full": {**source_fields, **target_fields}}
    image_from = str(source["id"]) if not has_image(dest) and has_image(source) else None
    return {"ok": True, "ids": [str(p["id"]) for p in people], "destination": destination,
            "source": str(source["id"]), "name": dest["name"], **match,
            "token": snapshot_token(people, destination), "people": [summary(p) for p in people],
            "values": values, "filled_fields": filled, "field_conflicts": conflicts,
            "image_from": image_from, "keeps_destination_photo": has_image(dest)}


def prepare(client, args: dict) -> dict:
    ids, destination = pair_ids(args)
    return plan(load_pair(client, ids), destination)


def merge(client, root: Path, args: dict) -> dict:
    ids, destination = pair_ids(args)
    if args.get("confirmed") is not True or not re.fullmatch(r"[a-f0-9]{64}", str(args.get("token") or "")):
        raise ValueError("미리보기를 확인하고 병합에 동의해야 합니다.")
    with ExitStack() as locks:
        for identifier in ids:
            locks.enter_context(entity_images.entity_lock(root, "performer", identifier))
        before = load_pair(client, ids)
        preview = plan(before, destination)
        if args["token"] != preview["token"]:
            raise ValueError("미리보기 이후 배우 정보가 바뀌었습니다. 다시 검사하세요.")
        if preview["confidence"] == "review" and args.get("reviewed_identity") is not True:
            raise ValueError("이름만 일치하는 후보입니다. 동일 배우인지 직접 확인해야 합니다.")
        history_path = root / "performer_merges" / (uuid.uuid4().hex + ".json")
        record = {"algorithm": ALGORITHM, "time": time.time(), "status": "preparing", "before": before, "plan": preview}
        backup, backup_status = None, "not_requested"
        try:
            write_json(history_path, record)  # Refuse a merge if its history cannot be saved.
            # A complete Stash backup is required, including separately stored image blobs.
            backup_status = "unconfirmed"
            response = client.graphql("mutation OneClickMergeBackup($input: BackupDatabaseInput!) { backupDatabase(input: $input) }",
                                      {"input": {"download": False, "includeBlobs": True}}, timeout=300)
            # Stash 0.31.1 returns explicit null on successful server-side backup
            # (download=false), NOT its filesystem path. GraphQL errors are raised
            # by StashClient. A missing field is not the same as explicit null.
            if not isinstance(response, dict) or "backupDatabase" not in response:
                raise ValueError("Stash 백업 완료 응답이 없어 병합하지 않았습니다.")
            backup_value = response["backupDatabase"]
            if backup_value is not None and (not isinstance(backup_value, str) or not backup_value.strip()):
                raise ValueError("Stash 백업 응답 형식을 확인하지 못해 병합하지 않았습니다.")
            backup = backup_value
            backup_status = "completed"
            record.update(backup=backup, backup_status=backup_status, status="backed_up")
            write_json(history_path, record)
            values = dict(preview["values"])
            if preview["image_from"]:
                values["image"] = client.performer_image_data(preview["image_from"])
            # Backup/image retrieval can be slow: check for intervening manual edits again.
            if snapshot_token(load_pair(client, ids), destination) != preview["token"]:
                raise ValueError("백업 중 배우 정보가 바뀌었습니다. 다시 검사하세요.")
            record["status"] = "merge_requested"
            write_json(history_path, record)
        except Exception as exc:
            # No performerMerge request has been sent: unlike a lost merge response,
            # this is a definite non-merge, even if the backup response was lost.
            record.update(status="not_merged", backup_status=backup_status, error=str(exc))
            try:
                write_json(history_path, record)
            except OSError:
                pass
            return {"ok": False, "status": "not_merged", "backup": backup, "backup_status": backup_status,
                    "history": str(history_path), "message": "병합하지 않았습니다. " + str(exc), "error": str(exc)}
        try:
            response = client.graphql("""mutation OneClickDuplicateMerge($input: PerformerMergeInput!) {
              performerMerge(input: $input) { id }
            }""", {"input": {"source": [preview["source"]], "destination": destination, "values": values}})
            if str((response.get("performerMerge") or {}).get("id")) != destination:
                raise ValueError("Stash 병합 응답을 확인하지 못했습니다.")
        except Exception as exc:
            record.update(status="needs_check", error=str(exc))
            try:
                write_json(history_path, record)
            except OSError:
                pass
            # Never retry an ambiguous destructive operation automatically.
            return {"ok": False, "status": "needs_check", "backup": backup, "backup_status": backup_status, "history": str(history_path),
                    "message": "병합 결과를 확정하지 못했습니다. 자동 재시도하지 않습니다. Stash에서 두 배우를 확인한 뒤 다시 검사하세요.",
                    "error": str(exc)}
        record["status"] = "merged"
        warning = None
        try:
            write_json(history_path, record)
        except OSError:
            warning = "병합은 완료됐지만 최종 이력 저장에 실패했습니다. Stash 백업은 보존되어 있습니다."
        return {"ok": True, "status": "merged", "destination": destination, "source": preview["source"],
                "name": preview["name"], "backup": backup, "backup_status": backup_status,
                "history": str(history_path), "warning": warning}
