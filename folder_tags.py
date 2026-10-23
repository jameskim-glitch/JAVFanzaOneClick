"""Add existing exact-name folder tags. Never create tags or replace tag lists."""
import re
import time
import unicodedata
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath

import maintenance
from performer_names import write_json

FIELDS = "id files { path } tags { id }"


def key(value):
    return unicodedata.normalize("NFC", str(value or "")).casefold()


def path_value(value):
    text = str(value or "")
    if ".." in re.split(r"[\\/]", text):
        return None
    cls = PureWindowsPath if "\\" in text or re.match(r"^[A-Za-z]:", text) else PurePosixPath
    result = cls(text)
    return result if result.is_absolute() else None


def folder_names(scene, roots):
    names = set()
    for item in scene.get("files") or []:
        file_path = path_value(item.get("path"))
        if file_path is None:
            continue
        for raw_root in roots:
            root = path_value(raw_root)
            if root is None or type(root) is not type(file_path):
                continue
            try:
                relative = file_path.relative_to(root)
            except ValueError:
                continue
            if not relative.parts:
                continue
            names.update(key(n) for n in (root.name, *relative.parts[:-1]) if n)
    return names


def context(client):
    roots = client.graphql("query OneClickFolderRoots { configuration { general { stashes { path } } } }")["configuration"]["general"]["stashes"]
    tags, page, expected = [], 1, None
    while True:
        result = client.graphql("query OneClickFolderTags($page:Int!){findTags(filter:{page:$page,per_page:100,sort:\"id\",direction:ASC}){count tags{id name ignore_auto_tag}}}", {"page": page})["findTags"]
        if expected is not None and expected != result["count"]:
            raise ValueError("태그 목록이 변경되었습니다. 다시 실행하세요.")
        expected = result["count"]
        tags.extend(result["tags"])
        if len(tags) >= expected:
            break
        if not result["tags"]:
            raise ValueError("태그 목록을 끝까지 읽지 못했습니다.")
        page += 1
    if len(tags) != expected or len({t["id"] for t in tags}) != expected:
        raise ValueError("태그 목록에 중복 또는 누락이 있습니다.")
    # Include ignored names in collision checks so a normalized duplicate cannot
    # silently select a different tag when one spelling is excluded.
    groups = {}
    for tag in tags:
        groups.setdefault(key(tag["name"]), []).append(tag)
    return {"roots": [r["path"] for r in roots],
            "tags": {name: rows[0] for name, rows in groups.items() if name and len(rows) == 1 and not rows[0]["ignore_auto_tag"]},
            "ambiguous_names": [name for name, rows in groups.items() if len(rows) > 1]}


def missing(scene, settings):
    existing = {t["id"] for t in scene.get("tags") or []}
    names = folder_names(scene, settings["roots"])
    return sorted([t for name, t in settings["tags"].items() if name in names and t["id"] not in existing], key=lambda t: int(t["id"]))


def get_scene(client, identifier):
    if not re.fullmatch(r"[0-9]{1,12}", str(identifier)):
        raise ValueError("장면 ID가 올바르지 않습니다.")
    return client.graphql("query OneClickFolderScene($id:ID!){findScene(id:$id){" + FIELDS + "}}", {"id": str(identifier)})["findScene"]


def scenes(client):
    rows, page, expected = [], 1, None
    while True:
        data = client.graphql("query OneClickFolderScenes($page:Int!){findScenes(filter:{page:$page,per_page:100,sort:\"id\",direction:ASC}){count scenes{" + FIELDS + "}}}", {"page": page})["findScenes"]
        if expected is not None and expected != data["count"]:
            raise ValueError("장면 목록이 변경되었습니다. 스캔이 끝난 뒤 다시 실행하세요.")
        expected = data["count"]
        rows.extend(data["scenes"])
        if len(rows) >= expected:
            break
        if not data["scenes"]:
            raise ValueError("장면 목록 누락")
        page += 1
    if len(rows) != expected or len({s["id"] for s in rows}) != expected:
        raise ValueError("장면 목록 중복")
    return rows


def plan(client):
    settings = context(client)
    rows = scenes(client)
    items = [{"id": s["id"], "tags": [{"id": t["id"], "name": t["name"]} for t in missing(s, settings)]} for s in rows]
    items = [item for item in items if item["tags"]]
    return {"ok": True, "checked": len(rows), "pending": len(items), "items": items, "ambiguous_names": settings["ambiguous_names"]}


def apply_one(client, root, identifier, backup=None):
    with maintenance.operation_lock(root, "folder-tags-" + str(identifier)):
        current = get_scene(client, identifier)
        if current is None:
            return {"id": str(identifier), "status": "gone"}
        additions = missing(current, context(client))
        if not additions:
            return {"id": str(identifier), "status": "unchanged"}
        if backup:
            backup.ensure()
            # A full backup can be slow. Re-check paths, existing tags and rules.
            current = get_scene(client, identifier)
            if current is None:
                return {"id": str(identifier), "status": "gone"}
            additions = missing(current, context(client))
            if not additions:
                return {"id": str(identifier), "status": "unchanged"}
        path = Path(root) / "folder_tags" / (uuid.uuid4().hex + ".json")
        added_ids = [t["id"] for t in additions]
        record = {"time": time.time(), "status": "prepared", "id": str(identifier), "before": current, "added_ids": added_ids}
        write_json(path, record)
        # Re-read before mutation; never apply a category from an obsolete path.
        if get_scene(client, identifier) != current:
            record["status"] = "changed"
            write_json(path, record)
            return {"id": str(identifier), "status": "changed"}
        record["status"] = "update_requested"
        write_json(path, record)
        try:
            result = client.graphql("mutation OneClickFolderTagAdd($input:BulkSceneUpdateInput!){bulkSceneUpdate(input:$input){id tags{id}}}",
                                    {"input": {"ids": [str(identifier)], "tag_ids": {"ids": added_ids, "mode": "ADD"}}})["bulkSceneUpdate"]
            required = {t["id"] for t in current.get("tags") or []} | set(added_ids)
            if not isinstance(result, list) or len(result) != 1 or result[0]["id"] != str(identifier) or not required.issubset({t["id"] for t in result[0]["tags"]}):
                raise RuntimeError("태그 추가 응답을 확인하지 못했습니다.")
            after = get_scene(client, identifier)
            if after is None or not required.issubset({t["id"] for t in after.get("tags") or []}):
                raise RuntimeError("저장된 태그를 확인하지 못했습니다.")
            record.update(status="updated", after=after)
            write_json(path, record)
            return {"id": str(identifier), "status": "updated", "added": [t["name"] for t in additions], "added_ids": added_ids}
        except Exception as exc:
            record.update(status="needs_check", error=str(exc))
            write_json(path, record)
            raise RuntimeError(f"태그 저장 결과 확인이 필요합니다. 자동 재시도하지 않습니다. 기록: {path}") from exc


def run(client, root, dry_run=False, log=lambda message: None):
    if dry_run:
        return plan(client)
    with maintenance.operation_lock(root, "folder-tags-bulk"):
        preview = plan(client)
        backup = maintenance.Backup(client)
        result = {"ok": True, "checked": preview["checked"], "pending": preview["pending"], "updated": 0, "added": 0, "results": []}
        for index, item in enumerate(preview["items"], 1):
            try:
                outcome = apply_one(client, root, item["id"], backup)
                result["results"].append(outcome)
                if outcome["status"] == "updated":
                    result["updated"] += 1
                    result["added"] += len(outcome["added_ids"])
                log(f"폴더 태그: {index}/{preview['pending']} · #{item['id']} {outcome['status']}")
            except Exception as exc:
                result["ok"] = False
                result["results"].append({"id": item["id"], "status": "needs_check", "error": str(exc)})
                break
        result["backed_up"] = backup.done
        write_json(Path(root) / "folder_tags" / ("batch-" + uuid.uuid4().hex + ".json"), result)
        return result
