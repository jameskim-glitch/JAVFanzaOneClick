"""Match Japanese studio aliases against StashDB's actual studio directory."""
import json
import re
import time
import unicodedata
import urllib.request
import uuid
from urllib.parse import urlsplit
from contextlib import ExitStack
from pathlib import Path

import entity_images
import image_upgrade as hq
import maintenance
import stashdb_lookup as lookup
from performer_names import read_json, write_json

FIELDS = "id name aliases urls image_path updated_at stash_ids { endpoint stash_id } parent_studio { id } child_studios { id } tags { id } custom_fields details rating100 favorite ignore_auto_tag organized"

# Verified against official site titles on 2026-10-15. Both UUID and official
# domain must still match; a similarly named subsidiary is never substituted.
OFFICIAL_ALIASES = {
    "5f954e5b-0621-4375-a1fa-379283fe65c7": ("wanz-factory.com", ["ワンズファクトリー"]),
    "4ad32f28-dfe3-46c7-a434-a0173f59539a": ("moodyz.com", ["ムーディーズ"]),
    "d363f260-0a15-4336-92e2-d9228e12b532": ("naturalhigh.co.jp", ["ナチュラルハイ"]),
    "1f8ff270-d773-416d-b39c-248da61b62ce": ("s1s1s1.com", ["エスワン ナンバーワンスタイル"]),
    "20496dd0-b53c-4156-b982-8ce67b7a487c": ("muku.tv", ["無垢"]),
}


def source_names(source):
    names = [source["name"], *(source.get("aliases") or [])]
    bridge = OFFICIAL_ALIASES.get(source["id"])
    domains = {(urlsplit(u["url"]).hostname or "").removeprefix("www.") for u in source.get("urls") or []}
    if bridge and bridge[0] in domains:
        names.extend(bridge[1])
    if (source.get("parent") or {}).get("id") == "04e2f54d-12db-490b-91fe-460cf334f70c":
        names.extend([n + "/妄想族" for n in list(names)])
    return {key(n) for n in names} - {""}


def key(value):
    # Width/case/spacing only. Do not remove brand words, parent labels or digits.
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


class Directory:
    def __init__(self, client, root, log=lambda message: None):
        self.client, self.root = client, Path(root)
        self.log = log

    def load(self, force=False):
        path = self.root / "studio_directory.json"
        cached = read_json(path)
        if not force and cached.get("revision") == 1 and 0 <= time.time() - cached.get("time", 0) < 7 * 86400:
            return cached["studios"]
        boxes = self.client.graphql("query OneClickStudioConnection { configuration { general { stashBoxes { endpoint api_key max_requests_per_minute } } } }")["configuration"]["general"]["stashBoxes"]
        box = next((b for b in boxes if str(b.get("endpoint", "")).rstrip("/").lower() == lookup.ENDPOINT), None)
        if not box or not box.get("api_key"):
            raise ValueError("Stash 설정에서 StashDB 연결과 API 키를 먼저 등록하세요.")
        # Credentials are used in memory for the configured fixed endpoint only.
        # Never serialize them or allow redirects to a different server.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError("StashDB 연결 주소가 변경되어 요청을 중단했습니다.")
        opener = urllib.request.build_opener(NoRedirect())
        headers = {"Content-Type": "application/json", "ApiKey": box["api_key"], "User-Agent": "JAVFanzaOneClick/1.10.0"}
        interval = max(0.3, 60 / max(1, int(box.get("max_requests_per_minute") or 120)))
        rows, page, expected = [], 1, None
        query = "query OneClickStudioDirectory($page:Int!){queryStudios(input:{page:$page,per_page:100,sort:NAME,direction:ASC}){count studios{id name aliases deleted urls{url} parent{id}}}}"
        while True:
            if page > 1:
                time.sleep(interval)
            req = urllib.request.Request(lookup.ENDPOINT, data=json.dumps({"query": query, "variables": {"page": page}}).encode(), headers=headers, method="POST")
            with opener.open(req, timeout=30) as response:
                payload = json.loads(response.read(4_000_000))
            if payload.get("errors"):
                raise ValueError("StashDB 제작사 목록 조회가 실패했습니다. 잠시 후 다시 실행하세요.")
            data = payload["data"]["queryStudios"]
            if expected is not None and data["count"] != expected:
                raise ValueError("조회 도중 StashDB 제작사 목록이 바뀌었습니다. 다시 실행하세요.")
            expected = data["count"]
            if expected > 50000:
                raise ValueError("제작사 목록 크기가 예상 범위를 넘었습니다.")
            rows.extend(data["studios"])
            if page == 1 or page % 10 == 0 or len(rows) >= expected:
                self.log(f"StashDB 제작사 목록: {len(rows)}/{expected} (최초 조회 후 7일 캐시)")
            if len(rows) >= expected:
                break
            if not data["studios"]:
                raise ValueError("제작사 목록을 끝까지 읽지 못했습니다.")
            page += 1
        if len(rows) != expected or len({r["id"] for r in rows}) != expected:
            raise ValueError("제작사 목록에 중복 또는 누락이 있습니다. 다시 조회하세요.")
        rows = [r for r in rows if not r.get("deleted")]
        write_json(path, {"revision": 1, "time": time.time(), "studios": rows})
        return rows


def local_studios(client):
    rows, page, expected = [], 1, None
    while True:
        data = client.graphql("query OneClickStudios($page:Int!){findStudios(filter:{page:$page,per_page:100,sort:\"id\",direction:ASC}){count studios{" + FIELDS + "}}}", {"page": page})["findStudios"]
        if expected is not None and expected != data["count"]:
            raise ValueError("제작사 목록이 변경되었습니다. 다시 실행하세요.")
        expected = data["count"]
        rows.extend(data["studios"])
        if len(rows) >= expected:
            break
        if not data["studios"]:
            raise ValueError("제작사 목록 누락")
        page += 1
    if len({r["id"] for r in rows}) != expected:
        raise ValueError("제작사 목록 중복")
    return rows


def resolve(local, directory):
    linked = hq.remote_id(local)
    if linked:
        choices = [r for r in directory if r["id"] == linked]
    else:
        names = {key(n) for n in [local["name"], *(local.get("aliases") or [])]} - {""}
        choices = [r for r in directory if names & source_names(r)]
    if len(choices) != 1:
        return {"status": "unmatched" if not choices else "ambiguous", "candidates": len(choices)}
    source = choices[0]
    remote = {"urls": [u["url"] for u in source.get("urls") or []]}
    if lookup.conflict(local, remote, "studio"):
        return {"status": "conflict", "reason": "FANZA 제작사 ID 불일치"}
    return {"status": "matched", "source": source}


def values_for(local, source):
    values = {"id": local["id"]}
    if local["name"] != source["name"]:
        values["name"] = source["name"]
    aliases = list(dict.fromkeys(n for n in [*(local.get("aliases") or []), local["name"], *(source.get("aliases") or [])]
                                if n and key(n) != key(source["name"])))
    if aliases != (local.get("aliases") or []):
        values["aliases"] = aliases
    if not hq.remote_id(local):
        values["stash_ids"] = [*(local.get("stash_ids") or []), {"endpoint": lookup.ENDPOINT, "stash_id": source["id"]}]
    urls = list(dict.fromkeys([*(local.get("urls") or []), *[u["url"] for u in source.get("urls") or []]]))
    if urls != (local.get("urls") or []):
        values["urls"] = urls
    return values


def avoid_name_collisions(values, local, rows):
    """Stash enforces uniqueness across studio names AND aliases."""
    claimed = {key(n) for r in rows if r["id"] != local["id"] for n in [r["name"], *(r.get("aliases") or [])]}
    if key(values.get("name")) in claimed:
        values.pop("name", None)
    if "aliases" in values:
        values["aliases"] = [n for n in values["aliases"] if key(n) not in claimed and key(n) != key(values.get("name", local["name"]))]
        if values["aliases"] == (local.get("aliases") or []):
            values.pop("aliases")
    return values


def plan(client, root, force=False, log=lambda message: None):
    directory = Directory(client, root, log).load(force)
    rows = local_studios(client)
    results = []
    for local in rows:
        try:
            resolved = resolve(local, directory)
            result = {"id": local["id"], "before": local["name"], **resolved}
            if resolved["status"] == "matched":
                result["values"] = values_for(local, resolved["source"])
            results.append(result)
        except ValueError as exc:
            results.append({"id": local["id"], "before": local["name"], "status": "conflict", "reason": str(exc)})
    groups = {}
    for result in results:
        if result["status"] == "matched":
            groups.setdefault(result["source"]["id"], []).append(result["id"])
    return {"ok": True, "rows": rows, "results": results, "duplicate_groups": [ids for ids in groups.values() if len(ids) > 1], "total": len(rows)}


def run(client, root, args, log=lambda message: None, backup=None):
    if args.get("confirmed") is not True:
        raise ValueError("제작사 정리에 동의해야 합니다.")
    with maintenance.operation_lock(root, "studio-sync"):
        preview = plan(client, root, args.get("force", False), log)
        if any(r["name"].startswith("__OneClick_merge_") for r in preview["rows"]):
            raise ValueError("이전 병합의 임시 제작사가 남아 있습니다. 복구 기록과 현재 항목을 확인한 뒤 진행하세요.")
        backup = backup or maintenance.Backup(client)
        history = Path(root) / "studio_sync" / (uuid.uuid4().hex + ".json")
        record = {"time": time.time(), "before": preview["rows"], "plan": preview["results"], "results": []}
        write_json(history, record)
        for result in preview["results"]:
            if result["status"] != "matched":
                record["results"].append({k: v for k, v in result.items() if k != "source"})
                continue
            identifier, source = result["id"], result["source"]
            try:
                with entity_images.entity_lock(root, "studio", identifier):
                    current = get_studio(client, identifier)
                    original = next(r for r in preview["rows"] if r["id"] == identifier)
                    if current != original:
                        raise ValueError("조회 이후 제작사 정보가 바뀌었습니다.")
                    values = values_for(current, source)
                    values = avoid_name_collisions(values, current, local_studios(client))
                    if len(values) == 1:
                        status = "unchanged"
                    else:
                        backup.ensure()
                        if get_studio(client, identifier) != current:
                            raise ValueError("백업 중 제작사 정보가 변경되어 저장하지 않았습니다.")
                        record["pending"] = {"id": identifier, "values": values}
                        write_json(history, record)
                        response = client.graphql("mutation OneClickStudioSync($input:StudioUpdateInput!){studioUpdate(input:$input){id}}", {"input": values})
                        if str((response.get("studioUpdate") or {}).get("id")) != identifier:
                            raise RuntimeError("제작사 저장 응답을 확인하지 못했습니다.")
                        status = "updated"
                    record["results"].append({"id": identifier, "status": status, "name": values.get("name", current["name"])})
                    record.pop("pending", None)
                    write_json(history, record)
                    log(f"제작사 #{identifier}: {status}")
            except Exception as exc:
                record["results"].append({"id": identifier, "status": "needs_check", "error": str(exc)})
                write_json(history, record)
                break
        ok = not any(r["status"] == "needs_check" for r in record["results"])
        merges = []
        if ok and args.get("merge_duplicates") is True:
            for ids in preview["duplicate_groups"]:
                try:
                    merges.append(merge_group(client, root, ids, backup))
                    log("동일 제작사 병합: " + ", ".join(ids))
                except Exception as exc:
                    merges.append({"ids": ids, "status": "needs_check", "error": str(exc)})
                    ok = False
                    break
        record["merges"] = merges
        write_json(history, record)
        return {"ok": ok, "results": record["results"], "merges": merges, "history": str(history), "duplicate_groups": preview["duplicate_groups"], "backed_up": backup.done}


def get_studio(client, identifier):
    result = client.graphql("query OneClickStudio($id:ID!){findStudio(id:$id){" + FIELDS + "}}", {"id": identifier}).get("findStudio")
    if not result:
        raise ValueError("제작사 항목이 없어졌습니다.")
    return result


def references(client, kind, identifier):
    collection = {"Scene": "scenes", "Image": "images", "Gallery": "galleries", "Group": "groups"}[kind]
    rows, page = [], 1
    while True:
        result = client.graphql(f"query OneClickStudioReferences($filter:{kind}FilterType!,$page:Int!){{find{collection.capitalize()}(filter:{{page:$page,per_page:100,sort:\"id\",direction:ASC}},{kind.lower()}_filter:$filter){{count {collection}{{id studio{{id}}}}}}}}",
                                {"filter": {"studios": {"value": [identifier], "modifier": "INCLUDES", "depth": 0}}, "page": page})["find" + collection.capitalize()]
        rows.extend(r["id"] for r in result[collection] if (r.get("studio") or {}).get("id") == identifier)
        if page * 100 >= result["count"] or not result[collection]:
            break
        page += 1
    return rows


def merge_group(client, root, ids, backup):
    ids = sorted(set(ids), key=int)
    if len(ids) < 2:
        raise ValueError("제작사 병합 대상이 부족합니다.")
    with ExitStack() as locks:
        for identifier in ids:
            locks.enter_context(entity_images.entity_lock(root, "studio", identifier))
        people = [get_studio(client, i) for i in ids]
        remote = {hq.remote_id(p) for p in people}
        if None in remote or len(remote) != 1:
            raise ValueError("같은 StashDB 제작사 ID가 확인된 항목만 병합합니다.")
        # Re-fetch identity from StashDB before any destructive step.
        source = hq.source_record(client, "studio", people[0])
        destination = next((p for p in people if key(p["name"]) == key(source["name"])), people[0])
        dest_id = destination["id"]
        parents = {(p.get("parent_studio") or {}).get("id") for p in people} - {None, *ids}
        if len(parents) > 1 or any(p.get("custom_fields") for p in people):
            raise ValueError("상위 제작사 또는 사용자 정의 필드 확인이 필요하여 병합하지 않았습니다.")
        if any((p.get("parent_studio") or {}).get("id") in ids for p in people):
            raise ValueError("서로 상하위 관계인 제작사는 자동 병합하지 않습니다.")
        urls = list(dict.fromkeys(u for p in people for u in p.get("urls") or []))
        aliases = list(dict.fromkeys(n for p in people for n in [p["name"], *(p.get("aliases") or [])] if key(n) != key(source["name"])))
        stash_ids = []
        by_endpoint = {}
        for p in people:
            for value in p.get("stash_ids") or []:
                by_endpoint.setdefault(value["endpoint"].rstrip("/"), set()).add(value["stash_id"])
                if value not in stash_ids:
                    stash_ids.append(value)
        if any(len(values) > 1 for values in by_endpoint.values()):
            raise ValueError("다른 출처 식별번호가 충돌하여 병합하지 않았습니다.")
        notes = list(dict.fromkeys(p["details"] for p in people if p.get("details")))
        values = {"id": dest_id, "name": source["name"], "aliases": aliases, "urls": urls, "stash_ids": stash_ids,
                  "tag_ids": list(dict.fromkeys(t["id"] for p in people for t in p.get("tags") or [])),
                  "favorite": any(p.get("favorite") for p in people), "organized": any(p.get("organized") for p in people),
                  "ignore_auto_tag": any(p.get("ignore_auto_tag") for p in people)}
        if notes:
            values["details"] = "\n\n".join(notes)
        ratings = [p["rating100"] for p in people if p.get("rating100") is not None]
        if ratings:
            values["rating100"] = max(ratings)
        values["parent_id"] = next(iter(parents)) if parents else None
        values = avoid_name_collisions(values, destination, local_studios(client))
        if entity_images.missing_image(destination):
            donor = next((p for p in people if not entity_images.missing_image(p)), None)
            if donor:
                values["image"] = client.entity_image_data("studio", donor["id"])
        moves = {p["id"]: {kind: references(client, kind, p["id"]) for kind in ("Scene", "Image", "Gallery", "Group")} for p in people if p["id"] != dest_id}
        path = Path(root) / "studio_merges" / (uuid.uuid4().hex + ".json")
        record = {"time": time.time(), "before": people, "destination": dest_id, "moves": moves, "status": "prepared", "deleted": []}
        write_json(path, record)
        backup.ensure()
        if people != [get_studio(client, i) for i in ids]:
            raise ValueError("백업 중 제작사 정보가 변경되어 병합하지 않았습니다.")
        record["status"] = "updating"
        write_json(path, record)
        try:
            response = client.graphql("mutation OneClickStudioMergeValues($input:StudioUpdateInput!){studioUpdate(input:$input){id}}", {"input": values})
            if (response.get("studioUpdate") or {}).get("id") != dest_id:
                raise ValueError("대상 제작사 저장 결과 확인 필요")
            for source_id, kinds in moves.items():
                for kind, links in kinds.items():
                    for start in range(0, len(links), 100):
                        batch = links[start:start + 100]
                        field = "bulk" + kind + "Update"
                        result = client.graphql(f"mutation OneClickStudioMove($input:Bulk{kind}UpdateInput!){{{field}(input:$input){{id studio{{id}}}}}}", {"input": {"ids": batch, "studio_id": dest_id}}).get(field)
                        if not isinstance(result, list) or {r["id"] for r in result if (r.get("studio") or {}).get("id") == dest_id} != set(batch):
                            raise ValueError("연결 이동 결과를 확인하지 못했습니다.")
                child_ids = [c["id"] for p in people if p["id"] == source_id for c in p.get("child_studios") or [] if c["id"] not in ids]
                for child_id in child_ids:
                    child = get_studio(client, child_id)
                    if (child.get("parent_studio") or {}).get("id") != source_id:
                        raise ValueError("하위 제작사가 변경되었습니다.")
                    moved = client.graphql("mutation OneClickStudioChild($input:StudioUpdateInput!){studioUpdate(input:$input){id parent_studio{id}}}", {"input": {"id": child_id, "parent_id": dest_id}})["studioUpdate"]
                    if (moved.get("parent_studio") or {}).get("id") != dest_id:
                        raise ValueError("하위 제작사 이동 확인 필요")
                latest = get_studio(client, source_id)
                if any(references(client, kind, source_id) for kind in kinds) or latest.get("child_studios"):
                    raise ValueError("연결이 남아 있어 원본 제작사를 삭제하지 않았습니다.")
                if hq.remote_id(latest) != hq.remote_id(destination):
                    raise ValueError("병합 중 원본 식별번호가 바뀌었습니다.")
                original = next(p for p in people if p["id"] == source_id)
                if any(latest.get(k) != v for k, v in original.items() if k not in {"updated_at", "child_studios"}):
                    raise ValueError("병합 중 원본 정보가 변경되어 삭제하지 않았습니다.")
                for kind, links in kinds.items():
                    if not set(links).issubset(set(references(client, kind, dest_id))):
                        raise ValueError("대상 제작사에 모든 연결이 보존되지 않아 삭제하지 않았습니다.")
                # Release the source's globally unique name/aliases only AFTER
                # links are verified, then store them on the destination BEFORE
                # removing the source. The full original is in the journal/backup.
                staged_name = "__OneClick_merge_" + path.stem + "_" + source_id
                record["pending_source"] = {"id": source_id, "staged_name": staged_name, "before": latest}
                write_json(path, record)
                client.graphql("mutation OneClickStudioStage($input:StudioUpdateInput!){studioUpdate(input:$input){id}}", {"input": {"id": source_id, "name": staged_name, "aliases": []}})
                staged = get_studio(client, source_id)
                if staged["name"] != staged_name or staged.get("aliases"):
                    raise ValueError("중복 제작사의 별명 이전 준비를 확인하지 못했습니다.")
                target = get_studio(client, dest_id)
                transferred = list(dict.fromkeys([*(target.get("aliases") or []), original["name"], *(original.get("aliases") or [])]))
                transferred = [n for n in transferred if key(n) != key(target["name"])]
                client.graphql("mutation OneClickStudioAliases($input:StudioUpdateInput!){studioUpdate(input:$input){id}}", {"input": {"id": dest_id, "aliases": transferred}})
                target = get_studio(client, dest_id)
                if not {key(n) for n in transferred}.issubset({key(n) for n in target.get("aliases") or []}):
                    raise ValueError("기존 이름·별명 보관을 확인하지 못해 원본을 삭제하지 않았습니다.")
                if get_studio(client, source_id) != staged or any(references(client, kind, source_id) for kind in kinds):
                    raise ValueError("삭제 전 원본이 변경되었습니다.")
                deleted = client.graphql("mutation OneClickStudioRemove($input:StudioDestroyInput!){studioDestroy(input:$input)}", {"input": {"id": source_id}})
                if deleted.get("studioDestroy") is not True:
                    raise ValueError("중복 제작사 삭제 응답 확인 필요")
                record["deleted"].append(source_id)
                record.pop("pending_source", None)
                write_json(path, record)
            record["status"] = "merged"
            write_json(path, record)
        except Exception as exc:
            record.update(status="needs_check", error=str(exc))
            write_json(path, record)
            raise RuntimeError(f"일부 단계 결과 확인이 필요합니다. 자동 재시도하지 않습니다. 기록: {path}") from exc
        return {"status": "merged", "destination": dest_id, "deleted": record["deleted"], "history": str(path), "moved_scenes": sum(len(k["Scene"]) for k in moves.values())}
