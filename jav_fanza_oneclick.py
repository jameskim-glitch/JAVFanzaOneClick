#!/usr/bin/env python3
"""Self-contained Stash plugin backend for FANZA metadata and Korean details."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import fanza_core
import entity_images
import performer_names
import performer_duplicates
import image_upgrade
import stashdb_rescrape
import subtitles
import maintenance
import studio_sync
import folder_tags


VERSION = "1.12.0"
PLUGIN_ID = "JAVFanzaOneClick"
PAGE_SIZE = 100
TRANSLATION_REVISION = 2


def configure_stdio() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace", newline="\n")
            except (OSError, TypeError, ValueError):
                pass


configure_stdio()


def log(message: str) -> None:
    sys.stderr.write(f"[JAV FANZA OneClick {VERSION}] {message}\n")
    sys.stderr.flush()


def emit(output: Any = None, error: str | None = None) -> None:
    payload: dict[str, Any] = {}
    if error:
        payload["error"] = error
    else:
        payload["output"] = output
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.flush()


def read_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def pick(mapping: Any, *names: str, default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        if name in mapping:
            return mapping[name]
        if name.lower() in lowered:
            return lowered[name.lower()]
    return default


def plugin_dir(connection: dict[str, Any]) -> Path:
    configured = pick(connection, "PluginDir", "plugin_dir")
    if configured:
        return Path(str(configured)).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def normalized(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def hangul_count(value: Any) -> int:
    return len(re.findall(r"[가-힣]", str(value or "")))


def has_korean(value: Any, minimum: int = 5) -> bool:
    return hangul_count(value) >= minimum


def safe_name(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value or ""))[:120] or "unknown"


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".oneclick-cache-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = root / "data"

    def scene_path(self, scene_id: Any) -> Path:
        return self.root / "scenes" / f"{safe_name(scene_id)}.json"

    def translation_path(self, code: str, purpose: str) -> Path:
        return self.root / "translations" / f"{safe_name(code)}.{safe_name(purpose)}.json"

    def scene_complete(self, scene_id: Any, code: str) -> bool:
        try:
            data = json.loads(self.scene_path(scene_id).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            # Subtitles and entity images do not invalidate completed scene text.
            return data.get("version") in {"1.3.1", "1.4.0", "1.4.1", "1.5.0", "1.6.0", "1.7.0", "1.7.1", "1.8.0", "1.9.0", "1.9.1", "1.10.0", "1.11.0", VERSION} and data.get("code") == code and bool(data.get("complete"))
        except (OSError, ValueError, TypeError):
            return False

    def mark_scene(self, scene_id: Any, code: str, complete: bool, changes: list[str]) -> None:
        atomic_write(self.scene_path(scene_id), {
            "version": VERSION,
            "code": code,
            "complete": complete,
            "changes": changes,
            "updated_at": int(time.time()),
        })

    def cached_translation(self, code: str, source: str, purpose: str, minimum: int = 5) -> str | None:
        try:
            data = json.loads(self.translation_path(code, purpose).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
            translated = str(data.get("translated") or "")
            if (
                data.get("revision") == TRANSLATION_REVISION
                and data.get("source_sha256") == digest
                and has_korean(translated, minimum)
            ):
                return translated
        except (OSError, ValueError, TypeError):
            pass
        return None

    def cache_translation(self, code: str, source: str, translated: str, purpose: str) -> None:
        atomic_write(self.translation_path(code, purpose), {
            "revision": TRANSLATION_REVISION,
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "translated": translated,
            "updated_at": int(time.time()),
        })


class StashClient:
    def __init__(self, connection: dict[str, Any]) -> None:
        explicit = pick(connection, "Endpoint", "endpoint")
        if explicit:
            self.endpoint = str(explicit)
        else:
            scheme = str(pick(connection, "Scheme", "scheme", default="http") or "http")
            host = str(pick(connection, "Host", "host", default="127.0.0.1") or "127.0.0.1")
            if host in {"0.0.0.0", "::", "[::]", "localhost"}:
                host = "127.0.0.1"
            port = int(pick(connection, "Port", "port", default=9999) or 9999)
            self.endpoint = f"{scheme}://{host}:{port}/graphql"
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        cookie = pick(connection, "SessionCookie", "session_cookie", default={})
        cookie_name = pick(cookie, "Name", "name")
        cookie_value = pick(cookie, "Value", "value")
        if cookie_name and cookie_value:
            self.headers["Cookie"] = f"{cookie_name}={cookie_value}"
        api_key = pick(connection, "ApiKey", "api_key")
        if api_key:
            self.headers["ApiKey"] = str(api_key)

    def graphql(self, document: str, variables: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
        body = json.dumps({"query": document, "variables": variables or {}}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.endpoint, data=body, headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"Stash GraphQL HTTP {exc.code}: {raw[:500]}") from exc
        if payload.get("errors"):
            messages = "; ".join(str(item.get("message") or item) for item in payload["errors"])
            raise RuntimeError(f"Stash GraphQL error: {messages}")
        return payload.get("data") or {}

    def performer_image_data(self, identifier: str) -> str:
        return self.entity_image_data("performer", identifier)

    def entity_image_data(self, kind: str, identifier: str) -> str:
        """Copy a stored portrait from this Stash only; never forward auth externally."""
        import base64
        import io
        from PIL import Image
        if kind not in IMAGE_ENTITY_TYPES or not entity_images.numeric_id(identifier):
            raise ValueError("배우 이미지 ID가 올바르지 않습니다.")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError("Stash 사진 주소가 리디렉션되어 복사를 중단했습니다.")
        base = self.endpoint.rsplit("/graphql", 1)[0]
        request = urllib.request.Request(f"{base}/{kind}/{identifier}/image", headers=self.headers)
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            body = response.read(entity_images.MAX_IMAGE_BYTES + 1)
        if not body or len(body) > entity_images.MAX_IMAGE_BYTES:
            raise ValueError("기존 배우 사진 크기를 확인하지 못해 병합하지 않았습니다.")
        with Image.open(io.BytesIO(body)) as decoded:
            mime = entity_images.MIME_TYPES.get(decoded.format)
            if not mime or decoded.width * decoded.height > entity_images.MAX_PIXELS:
                raise ValueError("기존 배우 사진 형식을 확인하지 못해 병합하지 않았습니다.")
            decoded.verify()
        return f"data:{mime};base64," + base64.b64encode(body).decode("ascii")

    def find_scene(self, scene_id: Any) -> dict[str, Any] | None:
        data = self.graphql(SCENE_QUERY, {"id": str(scene_id)})
        scene = data.get("findScene")
        return scene if isinstance(scene, dict) else None

    def find_scenes(self, page: int) -> tuple[int, list[dict[str, Any]]]:
        data = self.graphql(SCENES_QUERY, {"filter": {
            "page": page,
            "per_page": PAGE_SIZE,
            "sort": "id",
            "direction": "ASC",
        }})
        result = data.get("findScenes") or {}
        return int(result.get("count") or 0), list(result.get("scenes") or [])

    def find_scenes_for_images(self, page, targets):
        conditions = []
        for kind, field in (("performer", "performers"), ("studio", "studios")):
            ids = [identifier for k, identifier in targets if k == kind]
            if ids:
                conditions.append({field: {"value": ids, "modifier": "INCLUDES", **({"depth": 0} if kind == "studio" else {})}})
        if not conditions:
            return 0, []
        data = self.graphql("query OneClickMissingImageScenes($filter: FindFilterType!, $scene: SceneFilterType!) { findScenes(filter:$filter,scene_filter:$scene) { count scenes { " + SCENE_FIELDS + " } } }",
                            {"filter": {"page": page, "per_page": PAGE_SIZE, "sort": "id", "direction": "ASC"}, "scene": {**conditions[0], **({"OR": conditions[1]} if len(conditions) > 1 else {})}})
        result = data.get("findScenes") or {}
        return int(result.get("count") or 0), list(result.get("scenes") or [])

    def find_studios(self, name: str) -> list[dict[str, Any]]:
        data = self.graphql(FIND_STUDIOS_QUERY, {"filter": {"q": name, "per_page": 100}})
        return list((data.get("findStudios") or {}).get("studios") or [])

    def scene_cover_missing(self, identifier):
        # Screenshot URLs are always present in Stash, even without a cover.
        data = self.graphql("query OneClickCoverState($id:Int!){findScenes(filter:{per_page:1},scene_filter:{id:{value:$id,modifier:EQUALS},is_missing:\"cover\"}){count}}", {"id": int(identifier)})
        return data["findScenes"]["count"] == 1

    def find_performers(self, name: str) -> list[dict[str, Any]]:
        data = self.graphql(FIND_PERFORMERS_QUERY, {"filter": {"q": name, "per_page": 100}})
        return list((data.get("findPerformers") or {}).get("performers") or [])

    def create_studio(self, name: str) -> str:
        data = self.graphql(CREATE_STUDIO_MUTATION, {"input": {"name": name}})
        return str(data["studioCreate"]["id"])

    def create_performer(self, person: dict[str, Any]) -> str:
        original_name = str(person.get("name_ja") or person.get("name") or "").strip()
        korean_name = str(person.get("name_ko") or original_name).strip()
        item: dict[str, Any] = {"name": korean_name, "gender": "FEMALE"}
        aliases = list(dict.fromkeys(value for value in [original_name, str(person.get("name_roman") or ""), *(person.get("name_aliases") or [])]
                                    if value and normalized(value) != normalized(korean_name)))
        if aliases:
            item["alias_list"] = aliases
        urls = [str(value) for value in person.get("urls") or [] if value]
        if urls:
            item["urls"] = urls
        data = self.graphql(CREATE_PERFORMER_MUTATION, {"input": item})
        return str(data["performerCreate"]["id"])

    def update_performer(self, performer_id: Any, name: str, aliases: list[str]) -> None:
        item = {"id": str(performer_id), "name": name, "alias_list": aliases}
        self.graphql(UPDATE_PERFORMER_MUTATION, {"input": item})

    def update_scene(self, update: dict[str, Any]) -> None:
        self.graphql(UPDATE_SCENE_MUTATION, {"input": update})

    def find_image_entity(self, kind: str, identifier: str) -> dict[str, Any] | None:
        entity_type, fields = IMAGE_ENTITY_TYPES[kind]
        data = self.graphql(f"query OneClickImageEntity($id: ID!) {{ find{entity_type}(id: $id) {{ {fields} }} }}",
                            {"id": identifier})
        entity = data.get(f"find{entity_type}")
        return entity if isinstance(entity, dict) else None

    def find_image_entities(self, kind: str, page: int) -> tuple[int, list[dict[str, Any]]]:
        entity_type, fields = IMAGE_ENTITY_TYPES[kind]
        collection = "performers" if kind == "performer" else "studios"
        data = self.graphql(f"query OneClickImageEntities($filter: FindFilterType!) {{ find{entity_type}s(filter: $filter) {{ count {collection} {{ {fields} }} }} }}",
                            {"filter": {"page": page, "per_page": PAGE_SIZE, "sort": "id", "direction": "ASC"}})
        result = data.get(f"find{entity_type}s") or {}
        return int(result.get("count") or 0), list(result.get(collection) or [])

    def update_entity_image(self, kind: str, identifier: str, image: str, stash_ids=None) -> None:
        entity_type, _ = IMAGE_ENTITY_TYPES[kind]
        item = {"id": identifier, "image": image}
        if stash_ids is not None:
            item["stash_ids"] = stash_ids
        result = self.graphql(f"mutation OneClickEntityImage($input: {entity_type}UpdateInput!) {{ {kind}Update(input: $input) {{ id }} }}",
                              {"input": item})
        if str((result.get(f"{kind}Update") or {}).get("id")) != str(identifier):
            raise RuntimeError("Stash 사진 저장 응답을 확인하지 못했습니다.")


SCENE_FIELDS = """
  id title code details urls date
  paths { screenshot }
  studio { id name aliases url urls image_path stash_ids { endpoint stash_id } }
  performers { id name alias_list urls image_path stash_ids { endpoint stash_id } }
  files { path basename duration }
"""
SCENE_QUERY = f"query Scene($id: ID!) {{ findScene(id: $id) {{ {SCENE_FIELDS} }} }}"
SCENES_QUERY = f"query Scenes($filter: FindFilterType) {{ findScenes(filter: $filter) {{ count scenes {{ {SCENE_FIELDS} }} }} }}"
FIND_STUDIOS_QUERY = """query Studios($filter: FindFilterType!) {
  findStudios(filter: $filter) { studios { id name aliases } }
}"""
FIND_PERFORMERS_QUERY = """query Performers($filter: FindFilterType!) {
  findPerformers(filter: $filter) { performers { id name alias_list urls } }
}"""
CREATE_STUDIO_MUTATION = """mutation Studio($input: StudioCreateInput!) {
  studioCreate(input: $input) { id }
}"""
CREATE_PERFORMER_MUTATION = """mutation Performer($input: PerformerCreateInput!) {
  performerCreate(input: $input) { id }
}"""
UPDATE_PERFORMER_MUTATION = """mutation Performer($input: PerformerUpdateInput!) {
  performerUpdate(input: $input) { id }
}"""
UPDATE_SCENE_MUTATION = """mutation Scene($input: SceneUpdateInput!) {
  sceneUpdate(input: $input) { id }
}"""
IMAGE_ENTITY_TYPES = {
    "performer": ("Performer", "id name alias_list urls image_path stash_ids { endpoint stash_id }"),
    "studio": ("Studio", "id name aliases url urls image_path stash_ids { endpoint stash_id }"),
}


def exact_entity(items: list[dict[str, Any]], name: str, alias_key: str) -> dict[str, Any] | None:
    wanted = normalized(name)
    for item in items:
        names = [item.get("name"), *(item.get(alias_key) or [])]
        if any(normalized(value) == wanted for value in names if value):
            return item
    return None


PROMOTION_MARKERS_JA = (
    "特典、セット商品情報", "特典・セット商品情報", "特典/セット商品情報",
    "特典付き商品、セット商品について", "特典が付属する商品、セット商品について",
)
PROMOTION_MARKERS_KO = (
    "혜택, 세트 상품 정보", "혜택·세트 상품 정보", "혜택/세트 상품 정보",
    "혜택이 포함된 상품, 세트 상품에 대해", "특전이 포함된 상품, 세트 상품에 대해",
)

CENSOR_SYMBOLS = "○〇●◯◉◎□■◇◆"
CENSOR_SYMBOLS += "×✕✖❌"
CENSOR_SYMBOL_PATTERN = f"[{re.escape(CENSOR_SYMBOLS)}]"

# FANZA descriptions sometimes replace one or more characters in well-known
# words with circles or squares. Only restore terms with an unambiguous,
# commonly used completion; unknown masked words are deliberately preserved.
CENSORED_JAPANESE_TERMS = (
    ("セ○クス", "セックス"), ("セッ○ス", "セックス"), ("セ○○ス", "セックス"),
    ("オ○ニー", "オナニー"), ("オナ○ー", "オナニー"), ("オ○○ー", "オナニー"),
    ("チ○ポ", "チンポ"), ("チ○コ", "チンコ"),
    ("ち○ぽ", "ちんぽ"), ("ち○こ", "ちんこ"),
    ("マ○コ", "マンコ"), ("ま○こ", "まんこ"), ("お○んこ", "おまんこ"),
    ("ア○ル", "アナル"),
    ("フ○ラ", "フェラ"), ("フェ○", "フェラ"),
    ("ク○ニ", "クンニ"), ("クン○", "クンニ"),
    ("パ○ズリ", "パイズリ"), ("パ○○リ", "パイズリ"),
    ("手○キ", "手コキ"), ("中○し", "中出し"),
    ("顔○", "顔射"), ("射○", "射精"),
    ("レ○プ", "レイプ"), ("潮○き", "潮吹き"),
    ("イ○マチオ", "イラマチオ"), ("イラ○チオ", "イラマチオ"),
    ("バ○ブ", "バイブ"), ("ロ○ター", "ローター"),
    ("デ○ルド", "ディルド"), ("ディ○ド", "ディルド"),
    ("ペ○ス", "ペニス"), ("ハ○撮り", "ハメ撮り"), ("ハメ○り", "ハメ撮り"),
    ("ザ○メン", "ザーメン"), ("ザー○ン", "ザーメン"),
    ("パ○パン", "パイパン"), ("ス○トロ", "スカトロ"),
    ("メ○イキ", "メスイキ"), ("ポ○チオ", "ポルチオ"),
    ("キ○タマ", "キンタマ"),
    ("お○ぱい", "おっぱい"), ("おっ○い", "おっぱい"), ("オ○パイ", "オッパイ"),
    ("ぶ○かけ", "ぶっかけ"), ("ぶっ○け", "ぶっかけ"),
    ("ご○くん", "ごっくん"),
    ("痴○", "痴漢"), ("強○", "強制"), ("犯○", "犯罪"),
    ("催○", "催眠"), ("輪○", "輪姦"), ("調○", "調教"),
    ("レ○プ", "レイプ"), ("ロ○ッ娘", "ロリっ娘"),
    ("女○生", "女学生"), ("J○", "JK"),
    ("○っ払い", "酔っ払い"),
)

CENSORED_CONTEXT_TERMS = (
    ("犯○れ", "犯され"), ("女子○生", "女子校生"), ("男子○生", "男子学生"),
    ("○漢", "痴漢"), ("凌○", "凌辱"), ("奴○", "奴隷"),
    ("昏○", "昏睡"), ("夜○い", "夜這い"), ("媚○", "媚薬"),
    ("制○姿", "制服姿"), ("トー○", "トー横"), ("歌○伎町", "歌舞伎町"),
    ("○内汚染", "膣内汚染"), ("性○罪", "性犯罪"),
    ("○ている所", "寝ている所"),
    ("○っぱら", "酔っぱら"), ("全員○す", "全員犯す"),
    ("コ○ケ", "コミケ"), ("ハ○公", "ハチ公"), ("虐○", "虐待"),
    ("睡眠○", "睡眠姦"), ("すい○ん姦", "すいみん姦"),
    ("ア○コ", "アソコ"), ("ツ○スター", "ツイスター"),
)

PROMOTION_HEADING_TERMS_JA = (
    "特典", "セット商品", "キャンペーン", "セール", "予約購入", "予約特典",
    "購入特典", "商品情報", "期間限定", "ポイント還元", "プレゼント情報",
)
PROMOTION_HEADING_TERMS_KO = (
    "혜택", "특전", "세트 상품", "세트상품", "캠페인", "세일", "예약 구매",
    "예약구매", "예약 특전", "구매 특전", "상품 정보", "기간 한정", "포인트 환원",
)


def clean_html_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|li|tr|h[1-6])\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def is_promotional_heading(value: str) -> bool:
    heading = " ".join(str(value or "").split())
    return any(term in heading for term in (*PROMOTION_HEADING_TERMS_JA, *PROMOTION_HEADING_TERMS_KO))


def remove_promotional_preamble(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    early = text[:500]
    early_headings = list(re.finditer(r"【([^】]{1,100})】", early))
    first_heading_is_promo = bool(
        early_headings and is_promotional_heading(early_headings[0].group(1))
    )
    first_heading_start = early_headings[0].start() if early_headings else 160
    marker_at_start = any(
        0 <= early.find(marker) <= first_heading_start
        for marker in (*PROMOTION_MARKERS_JA, *PROMOTION_MARKERS_KO)
    )
    promo_seen = first_heading_is_promo or marker_at_start
    if not promo_seen:
        return text

    # FANZA usually places the sale/benefit notice in the first 【heading】 and
    # starts the actual synopsis at the next non-promotional 【heading】.
    headings = list(re.finditer(r"【([^】]{1,100})】", text))
    heading_promo_seen = False
    for match in headings:
        heading = match.group(1)
        if is_promotional_heading(heading):
            heading_promo_seen = True
            continue
        if heading_promo_seen:
            return text[match.start():].strip()

    # Some products omit a second heading but end the notice with one of these
    # fixed disclaimer/link phrases. Keep everything following the last one.
    boundary_patterns = (
        r"特典(?:が)?付(?:属|い)ている商品[、,]\s*セット商品について[。.]?",
        r"特典付き商品[、,]\s*セット商品について[。.]?",
        r"(?:あらかじめ|予め)ご了承ください[。.]",
    )
    boundaries: list[re.Match[str]] = []
    for pattern in boundary_patterns:
        boundaries.extend(re.finditer(pattern, text))
    if boundaries:
        boundary = max(boundaries, key=lambda item: item.end())
        remainder = text[boundary.end():].strip(" \n:：-—")
        if remainder:
            return remainder
    return ""


def promotional_line(value: str) -> bool:
    line = " ".join(str(value or "").split())
    if not line:
        return False
    strong_markers = (
        *PROMOTION_MARKERS_JA, *PROMOTION_MARKERS_KO,
        "期間限定ポイント", "ポイント還元", "購入者特典", "予約特典", "セット商品について",
        "기간 한정 포인트", "포인트 환원", "구매자 혜택", "예약 특전", "세트 상품 안내",
    )
    if any(marker in line for marker in strong_markers):
        return True
    if re.match(
        r"^(?:特典|セット商品|キャンペーン情報|セール情報|購入特典|予約特典|"
        r"혜택|특전|세트\s*상품|캠페인\s*정보|세일\s*정보|구매\s*특전|예약\s*특전)",
        line,
    ):
        return True
    topic_ja = r"(?:特典|セット商品|キャンペーン|セール)"
    signal_ja = r"(?:対象|期間|購入|予約|ポイント|プレゼント|付属|開催|実施|終了|詳細|こちら)"
    topic_ko = r"(?:혜택|특전|세트\s*상품|캠페인|세일)"
    signal_ko = r"(?:대상|기간|구매|예약|포인트|증정|포함|진행|실시|종료|자세히|확인)"
    return bool(
        re.search(fr"{topic_ja}.*{signal_ja}|{signal_ja}.*{topic_ja}", line)
        or re.search(fr"{topic_ko}.*{signal_ko}|{signal_ko}.*{topic_ko}", line)
    )


def remove_promotional_sections(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = remove_promotional_preamble(text)
    if not text:
        return ""

    # Remove promotional 【sections】 wherever FANZA places them, including
    # notices appended after the synopsis.
    section_pattern = re.compile(r"【([^】]{1,100})】")
    while True:
        headings = list(section_pattern.finditer(text))
        promo = next((item for item in headings if is_promotional_heading(item.group(1))), None)
        if not promo:
            break
        following = next((item for item in headings if item.start() > promo.start()), None)
        end = following.start() if following else len(text)
        text = (text[:promo.start()] + text[end:]).strip()

    lines = text.splitlines()
    if len(lines) > 1:
        text = "\n".join(line for line in lines if not promotional_line(line)).strip()
    sentences = re.split(r"(?<=[。！？!?])(?![。！？!?])\s*", text)
    if len(sentences) > 1:
        text = " ".join(sentence for sentence in sentences if not promotional_line(sentence)).strip()
    return text


def restore_censored_terms(value: Any) -> str:
    text = str(value or "")
    for masked, restored in (*CENSORED_CONTEXT_TERMS, *CENSORED_JAPANESE_TERMS):
        pattern = "".join(
            CENSOR_SYMBOL_PATTERN if character == "○" else re.escape(character)
            for character in masked
        )
        text = re.sub(pattern, restored, text)
    text = re.sub(fr"媚薬を{CENSOR_SYMBOL_PATTERN}ませ", "媚薬を飲ませ", text)
    text = re.sub(fr"強引に{CENSOR_SYMBOL_PATTERN}す", "強引に犯す", text)
    return text


def contains_censor_symbols(value: Any) -> bool:
    return bool(re.search(CENSOR_SYMBOL_PATTERN, str(value or "")))


def remove_censor_symbols(value: Any) -> str:
    text = re.sub(fr"\s*{CENSOR_SYMBOL_PATTERN}+\s*", "", str(value or ""))
    text = re.sub(r"\s+([、。,.!?！？:：;；])", r"\1", text)
    return " ".join(text.split()).strip()


def sanitize_details(value: Any) -> str:
    cleaned = clean_html_text(value)
    cleaned = remove_promotional_sections(cleaned)
    return " ".join(cleaned.split())


def finalize_translated_text(value: Any, purpose: str) -> str:
    text = clean_html_text(value)
    if purpose == "details":
        text = remove_promotional_sections(text)
    return remove_censor_symbols(text)


def clean_existing_text(value: Any, purpose: str) -> str:
    text = clean_html_text(value)
    if purpose == "details":
        text = remove_promotional_sections(text)
    # Existing Korean prose is user-editable: keep the spaces around removed symbols.
    text = re.sub(CENSOR_SYMBOL_PATTERN, "", text)
    text = re.sub(r"\s+([、。,.!?！？:：;；])", r"\1", text)
    return " ".join(text.split())


def split_for_translation(source: str, limit: int = 700) -> list[str]:
    source = " ".join(str(source or "").split())
    if len(source) <= limit:
        return [source] if source else []
    sentences = re.split(r"(?<=[。！？!?])", source)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + len(sentence) > limit:
            chunks.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        chunks.append(current)
    return chunks


def request_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={
        "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) JAVFanzaOneClick/{VERSION}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def translate_google(source: str, minimum: int = 5) -> str | None:
    translated: list[str] = []
    for chunk in split_for_translation(source):
        params = urllib.parse.urlencode({
            "client": "gtx", "sl": "ja", "tl": "ko", "dt": "t", "q": chunk,
        })
        data = request_json("https://translate.googleapis.com/translate_a/single?" + params)
        if not isinstance(data, list) or not data or not isinstance(data[0], list):
            return None
        result = "".join(item[0] for item in data[0] if isinstance(item, list) and item and isinstance(item[0], str))
        if not result:
            return None
        translated.append(result)
    final = " ".join(" ".join(translated).split())
    return final if has_korean(final, minimum) else None


def translate_mymemory(source: str, minimum: int = 5) -> str | None:
    translated: list[str] = []
    # The service limit is UTF-8 bytes, not Python characters.
    chunks: list[str] = []
    chunk = ""
    size = 0
    for char in source:
        width = len(char.encode("utf-8"))
        if size + width > 480:
            chunks.append(chunk)
            chunk, size = "", 0
        chunk += char
        size += width
    if chunk:
        chunks.append(chunk)
    for chunk in chunks:
        params = urllib.parse.urlencode({"q": chunk, "langpair": "ja|ko"})
        data = request_json("https://api.mymemory.translated.net/get?" + params)
        if not isinstance(data, dict) or str(data.get("responseStatus", "200")) != "200":
            return None
        response = data.get("responseData")
        if not isinstance(response, dict) or not isinstance(response.get("translatedText"), str):
            return None
        result = html.unescape(response["translatedText"])
        if not result:
            return None
        translated.append(result)
    final = " ".join(" ".join(translated).split())
    return final if has_korean(final, minimum) else None


def korean_text(code: str, source: str, store: StateStore, purpose: str, minimum: int = 5) -> str | None:
    if not source:
        return None
    if has_korean(source, minimum):
        cleaned_source = finalize_translated_text(source, purpose)
        return cleaned_source if has_korean(cleaned_source, minimum) else None
    cached = store.cached_translation(code, source, purpose, minimum)
    if cached:
        cleaned_cached = finalize_translated_text(cached, purpose)
        return cleaned_cached if has_korean(cleaned_cached, minimum) else None
    for translator in (translate_google, translate_mymemory):
        try:
            translated = translator(source, minimum)
            if translated:
                translated = finalize_translated_text(translated, purpose)
                if has_korean(translated, minimum):
                    store.cache_translation(code, source, translated, purpose)
                    return translated
        except (OSError, ValueError, TypeError, urllib.error.URLError) as exc:
            log(f"translation provider failed: {exc}")
    return None


def extract_code(scene: dict[str, Any]) -> str | None:
    # Folder names, notes, actor names and related links are not the video's identity.
    explicit = fanza_core.extract_code_from_text(scene.get("code"))
    if explicit:
        return explicit
    file_codes = set()
    for file in scene.get("files") or []:
        filename = str(file.get("basename") or file.get("path") or "").replace("\\", "/").rsplit("/", 1)[-1]
        code = fanza_core.extract_code_from_text(filename)
        if code:
            file_codes.add(code)
    if len(file_codes) > 1:
        raise ValueError("서로 다른 품번의 영상이 한 장면에 있습니다. 장면의 품번을 먼저 지정하세요.")
    return next(iter(file_codes)) if file_codes else fanza_core.extract_code_from_text(scene.get("title"))


def title_is_placeholder(scene: dict[str, Any], code: str) -> bool:
    title = str(scene.get("title") or "").strip()
    if not title or normalized(title) == normalized(code):
        return True
    for file in scene.get("files") or []:
        for key in ("basename", "path"):
            value = Path(str(file.get(key) or "")).name
            if normalized(title) in {normalized(value), normalized(Path(value).stem)}:
                return True
    return False


def has_japanese_kana(value: Any) -> bool:
    return bool(re.search(r"[ぁ-ゖァ-ヺー]", str(value or "")))


def has_cjk_ideograph(value: Any) -> bool:
    return bool(re.search(r"[㐀-䶿一-鿿]", str(value or "")))


def title_needs_translation(scene: dict[str, Any], code: str) -> bool:
    title = str(scene.get("title") or "").strip()
    return (
        title_is_placeholder(scene, code)
        or contains_censor_symbols(title)
        or (has_japanese_kana(title) and not has_korean(title, 1))
    )


def details_need_refresh(value: Any) -> bool:
    text = str(value or "")
    if not has_korean(text, 1):
        return True
    if re.search(r"(?i)<(?:br|p|div|span|li|table|tr|td)\b", text):
        return True
    if contains_censor_symbols(text):
        return True
    cleaned = clean_html_text(text)
    if clean_existing_text(cleaned, "details") != " ".join(cleaned.split()):
        return True
    return False


def performers_need_translation(scene: dict[str, Any]) -> bool:
    for person in scene.get("performers") or []:
        name = str(person.get("name") or "")
        if name and not has_korean(name, 1) and (has_japanese_kana(name) or has_cjk_ideograph(name)):
            return True
    return False


def needs_processing(scene: dict[str, Any], code: str, store: StateStore, force: bool = False) -> bool:
    if force:
        return True
    record = performer_names.read_json(store.scene_path(scene.get("id")))
    # Re-evaluate old completion flags using current fields, without fetching metadata.
    text_ready = not (details_need_refresh(scene.get("details")) or title_needs_translation(scene, code) or performers_need_translation(scene))
    if record.get("code") == code and text_ready and "표지" in record.get("changes", []):
        return False
    if record.get("version") == VERSION and record.get("code") == code and time.time() < record.get("retry_after", 0):
        fingerprint = hashlib.sha256(json.dumps({k: scene.get(k) for k in ("title", "details", "code", "performers", "paths")}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if record.get("fingerprint") == fingerprint:
            return False
    if not store.scene_complete(scene.get("id"), code):
        return True
    return (
        details_need_refresh(scene.get("details"))
        or title_needs_translation(scene, code)
        or performers_need_translation(scene)
    )


def find_or_create_studio(client: StashClient, name: str) -> str:
    matches = [s for s in client.find_studios(name) if exact_entity([s], name, "aliases")]
    if len(matches) > 1:
        raise ValueError("이름·별칭이 같은 제작사가 여러 곳입니다. 제작사 정리를 먼저 실행하세요.")
    match = matches[0] if matches else None
    return str(match["id"]) if match else client.create_studio(name)


def exact_entity_names(items: list[dict[str, Any]], names: list[str], alias_key: str) -> dict[str, Any] | None:
    wanted = {normalized(name) for name in names if name}
    for item in items:
        candidates = [item.get("name"), *(item.get(alias_key) or [])]
        if any(normalized(value) in wanted for value in candidates if value):
            return item
    return None


def match_performer(items: list[dict[str, Any]], person: dict[str, Any]) -> dict[str, Any] | None:
    source_id = str(person.get("remote_site_id") or "")
    strong = [p for p in items if source_id and entity_images.source_ids(p, "performer") == {source_id}]
    if strong:
        if len(strong) > 1:
            raise ValueError("같은 FANZA 식별번호의 배우가 여러 명입니다. 중복 항목을 먼저 확인하세요.")
        return strong[0]
    names = [str(person.get(key) or "") for key in ("name_ja", "name", "name_ko", "name_roman")]
    matches = [p for p in items if not entity_images.source_ids(p, "performer")
               and exact_entity_names([p], names, "alias_list")]
    if len(matches) > 1:
        raise ValueError("이름·별칭이 같은 배우가 여러 명입니다. 새 배우를 만들지 않았습니다.")
    return matches[0] if matches else None


def apply_performer_name(client: StashClient, match: dict[str, Any], person: dict[str, Any],
                         resolver: performer_names.Resolver | None = None) -> bool:
    original_name = str(person.get("name_ja") or person.get("name") or "").strip()
    korean_name = str(person.get("name_ko") or "").strip()
    if not korean_name:
        return False
    current_name = str(match.get("name") or "").strip()
    source_id = str(person.get("remote_site_id") or "")
    previous = []
    if resolver:
        previous = [resolver.legacy_name(source_id, name) for name in [original_name, *(person.get("source_names") or [])]]
        previous.append(resolver.applied_name(str(match["id"]), source_id))
    owned = any(value and normalized(current_name) == normalized(value) for value in previous)
    should_rename = (
        normalized(current_name) != normalized(korean_name)
        and (owned or (not has_korean(current_name, 1)
                       and (normalized(current_name) == normalized(original_name) or has_japanese_kana(current_name))))
    )
    target_name = korean_name if should_rename else current_name
    aliases: list[str] = []
    for value in [*(match.get("alias_list") or []), current_name if should_rename else "",
                  original_name, str(person.get("name_roman") or ""), *(person.get("name_aliases") or [])]:
        value = str(value or "").strip()
        if value and normalized(value) != normalized(target_name) and not any(normalized(a) == normalized(value) for a in aliases):
            aliases.append(value)
    changed = should_rename or aliases != list(match.get("alias_list") or [])
    if changed:
        client.update_performer(match["id"], target_name, aliases)
    if should_rename and resolver:
        try:
            resolver.mark_applied(str(match["id"]), source_id, target_name)
        except OSError as exc:
            log(f"performer name provenance could not be saved: {exc}")
    return changed


def ensure_performer(client: StashClient, person: dict[str, Any], scene_people: list[dict[str, Any]],
                     resolver: performer_names.Resolver | None = None) -> tuple[str, bool]:
    if resolver:
        identity = str(person.get("remote_site_id") or normalized(person.get("name_ja") or person.get("name")))
        with maintenance.operation_lock(resolver.root, "create-performer-" + identity):
            return _ensure_performer(client, person, scene_people, resolver)
    return _ensure_performer(client, person, scene_people, resolver)


def _ensure_performer(client, person, scene_people, resolver=None):
    original_name = str(person.get("name_ja") or person.get("name") or "").strip()
    names = [str(person.get("name_ko") or ""), original_name, str(person.get("name_roman") or "")]
    match = match_performer(scene_people, person)
    if not match:
        candidates: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for name in names:
            if not name:
                continue
            for candidate in client.find_performers(name):
                candidate_id = str(candidate.get("id") or "")
                if candidate_id and candidate_id not in seen_ids:
                    seen_ids.add(candidate_id)
                    candidates.append(candidate)
        match = match_performer(candidates, person)
    if not match:
        identifier = client.create_performer(person)
        if resolver and person.get("name_ko"):
            try:
                resolver.mark_applied(identifier, str(person.get("remote_site_id") or ""), person["name_ko"])
            except OSError as exc:
                log(f"performer name provenance could not be saved: {exc}")
        return identifier, False
    if resolver:
        with entity_images.entity_lock(resolver.root, "performer", str(match["id"])):
            latest = client.find_image_entity("performer", str(match["id"]))
            if not latest or not match_performer([latest], person):
                log(f"performer {match['id']} changed or could not be confirmed; preserving its name")
                return str(match["id"]), False
            return str(match["id"]), apply_performer_name(client, latest, person, resolver)
    return str(match["id"]), apply_performer_name(client, match, person, resolver)


def fetch_metadata(code: str, store: StateStore, force: bool = False) -> dict[str, Any]:
    metadata = fanza_core.fetch_metadata(code, force=force)
    if not metadata or not any(metadata.get(key) for key in ("title", "image", "details")):
        raise RuntimeError(f"FANZA에서 {code} 메타데이터를 찾지 못했습니다.")
    original_title = restore_censored_terms(str(metadata.get("title") or "").strip())
    metadata["title"] = original_title
    translated_title = korean_text(code, original_title, store, "title", minimum=1)
    if translated_title:
        metadata["title_ko"] = translated_title
    original_details = restore_censored_terms(sanitize_details(metadata.get("details")))
    metadata["details"] = original_details
    translated_details = korean_text(code, original_details, store, "details", minimum=1)
    if translated_details:
        metadata["details_ko"] = translated_details
    translated_people: list[dict[str, Any]] = []
    name_resolver = performer_names.Resolver(store.root)
    for index, person in enumerate(metadata.get("performers") or []):
        if not isinstance(person, dict):
            continue
        original_name = clean_html_text(person.get("name")).replace("\n", " ").strip()
        if not original_name:
            continue
        person = dict(person)
        person["name_ja"] = original_name
        remote_id = str(person.get("remote_site_id") or "").strip()
        try:
            name = name_resolver.profile(remote_id)
            if normalized(name["name_ja"]) != normalized(original_name):
                raise ValueError("FANZA 배우 이름과 작품의 배우 이름이 일치하지 않습니다.")
            person.update(name)
        except Exception as exc:
            # Never fall back to translating the meaning of a person's name.
            person.pop("name_ko", None)
            person["name_warning"] = str(exc)
            log(f"performer pronunciation unavailable ({remote_id or original_name}): {exc}")
        translated_people.append(person)
    metadata["performers"] = translated_people
    return metadata


def process_scene(client: StashClient, scene: dict[str, Any], store: StateStore, force: bool = False,
                  dry_run: bool = False, images: entity_images.Enricher | None = None, replace_cover: bool = False) -> dict[str, Any]:
    if dry_run:
        return _process_scene(client, scene, store, force, dry_run, images, replace_cover)
    with maintenance.operation_lock(store.root, "scene-" + str(scene.get("id"))):
        return _process_scene(client, scene, store, force, dry_run, images, replace_cover)


def _process_scene(client, scene, store, force=False, dry_run=False, images=None, replace_cover=False):
    scene_id = str(scene.get("id") or "")
    code = extract_code(scene)
    if not code:
        return {"ok": False, "skipped": True, "scene_id": scene_id, "error": "품번을 찾지 못했습니다."}
    if not needs_processing(scene, code, store, force):
        return {"ok": True, "skipped": True, "complete": True, "scene_id": scene_id, "code": code}

    metadata = fetch_metadata(code, store, force=force)
    update: dict[str, Any] = {"id": scene_id}
    changes: list[str] = []

    if normalized(scene.get("code")) != normalized(code):
        update["code"] = code
        changes.append("품번")
    original_title = str(metadata.get("title") or "").strip()
    translated_title = str(metadata.get("title_ko") or "").strip()
    current_title = str(scene.get("title") or "").strip()
    should_translate_title = (
        title_is_placeholder(scene, code)
        or contains_censor_symbols(current_title)
        or (original_title and normalized(current_title) == normalized(original_title))
        or (has_japanese_kana(current_title) and not has_korean(current_title, 1))
    )
    if has_korean(current_title, 1) and not title_is_placeholder(scene, code):
        cleaned_title = clean_existing_text(current_title, "title")
        if cleaned_title and cleaned_title != current_title:
            update["title"] = cleaned_title
            changes.append("제목 정리")
    elif translated_title and should_translate_title and normalized(current_title) != normalized(translated_title):
        update["title"] = translated_title
        changes.append("한국어 제목")
    if metadata.get("date") and not scene.get("date"):
        update["date"] = metadata["date"]
        changes.append("발매일")
    translated = str(metadata.get("details_ko") or "").strip()
    current_details = str(scene.get("details") or "")
    if has_korean(current_details, 1):
        cleaned_details = clean_existing_text(current_details, "details")
        if cleaned_details != current_details and cleaned_details:
            update["details"] = cleaned_details
            changes.append("기존 내용 정리")
    elif translated and details_need_refresh(current_details):
        update["details"] = translated
        changes.append("정리된 한국어 내용")
    urls = list(dict.fromkeys([
        *(str(value) for value in scene.get("urls") or [] if value),
        *(str(value) for value in metadata.get("urls") or [] if value),
    ]))
    if urls != list(scene.get("urls") or []):
        update["urls"] = urls
        changes.append("FANZA 링크")
    image = str(metadata.get("image") or "").strip()
    # An unknown cover state is preserved. Only explicit replacement or a known
    # missing cover permits a write; force refreshes text, not custom artwork.
    screenshot = (scene.get("paths") or {}).get("screenshot")
    cover_missing = "paths" in scene and (not screenshot or urllib.parse.parse_qs(urllib.parse.urlsplit(screenshot).query).get("default") == ["true"])
    if screenshot and image and not replace_cover:
        cover_missing = client.scene_cover_missing(scene_id) is True
    if image and (cover_missing or replace_cover):
        update["cover_image"] = image
        changes.append("표지")

    performer_names_changed = False
    name_resolver = performer_names.Resolver(store.root)
    if not dry_run:
        studio = metadata.get("studio") or {}
        studio_name = str(studio.get("name") or "").strip() if isinstance(studio, dict) else ""
        if studio_name and not scene.get("studio"):
            with maintenance.operation_lock(store.root, "create-studio-" + normalized(studio_name)):
                update["studio_id"] = find_or_create_studio(client, studio_name)
            changes.append("제작사")

        existing_ids = [str(item["id"]) for item in scene.get("performers") or [] if item.get("id")]
        performer_ids = list(existing_ids)
        for person in metadata.get("performers") or []:
            original_name = str(person.get("name_ja") or person.get("name") or "").strip()
            korean_name = str(person.get("name_ko") or original_name).strip()
            if not original_name:
                continue
            performer_id, renamed = ensure_performer(client, person, scene.get("performers") or [], name_resolver)
            performer_names_changed = performer_names_changed or renamed
            if performer_id not in performer_ids:
                performer_ids.append(performer_id)
        if performer_ids != existing_ids:
            update["performer_ids"] = performer_ids
            changes.append("배우")
        if performer_names_changed:
            changes.append("배우 이름·별칭")

    if dry_run:
        return {
            "ok": True, "dry_run": True, "scene_id": scene_id, "code": code,
            "changes": changes, "update": update, "metadata": metadata,
        }
    if len(update) > 1:
        latest = client.find_scene(scene_id)
        if isinstance(latest, dict):
            for field in ("title", "details", "urls", "date", "code", "studio", "performers", "paths"):
                # Own performer name edits are allowed; linked IDs must be stable.
                a, b = scene.get(field), latest.get(field)
                if field == "performers":
                    a, b = [p["id"] for p in a or []], [p["id"] for p in b or []]
                if a != b:
                    raise RuntimeError("처리 중 장면이 변경되었습니다. 다른 변경을 보존하고 이번 저장을 중단했습니다.")
        if "cover_image" in update and not replace_cover and client.scene_cover_missing(scene_id) is False:
            update.pop("cover_image")
            changes.remove("표지")
        if len(update) > 1:
            client.update_scene(update)
    performers_complete = all(
        has_korean(person.get("name_ko"), 1)
        for person in metadata.get("performers") or []
    )
    effective_scene = {**scene, **update}
    complete = bool((image or screenshot) and not details_need_refresh(effective_scene.get("details"))
                    and not title_needs_translation(effective_scene, code) and performers_complete)
    store.mark_scene(scene_id, code, complete, changes)
    if not complete:
        record = performer_names.read_json(store.scene_path(scene_id))
        # The next ordinary run backs off; a changed scene or force bypasses it.
        fresh = client.find_scene(scene_id)
        if isinstance(fresh, dict):
            record.update(retry_after=time.time() + 86400, fingerprint=hashlib.sha256(json.dumps(
                {k: fresh.get(k) for k in ("title", "details", "code", "performers", "paths")}, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
            atomic_write(store.scene_path(scene_id), record)
    image_result: dict[str, Any] = {}
    warning = None if complete else "표지·한국어 제목·내용·배우 이름 중 일부를 완성하지 못해 다음 일괄 작업에서 다시 시도합니다."
    try:
        images = images or entity_images.Enricher(client, store.root)
        image_scene = client.find_scene(scene_id)
        if isinstance(image_scene, dict):
            image_result = images.scene(image_scene, code, metadata)
            if image_result.get("portraits"):
                changes.append(f"배우 사진 {image_result['portraits']}개")
            if image_result.get("logos"):
                changes.append(f"제작사 로고 {image_result['logos']}개")
            if image_result.get("failed"):
                warning = " ".join(filter(None, [warning, "일부 사진·로고를 받지 못했습니다. 사진·로고 버튼으로 다시 시도하세요."]))
                log(f"scene {scene_id} entity images: {image_result['errors']}")
    except Exception as exc:
        # Optional image enrichment must not discard a successful metadata update.
        log(f"scene {scene_id} entity images failed: {exc}")
        warning = " ".join(filter(None, [warning, "사진·로고는 처리하지 못했습니다. 사진·로고 버튼으로 다시 시도하세요."]))
    return {
        "ok": True, "scene_id": scene_id, "code": code,
        "updated": len(update) > 1 or performer_names_changed or bool(image_result.get("filled")),
        "complete": complete, "changes": changes,
        "warning": warning, "entity_images": image_result,
    }


def scene_id_from_args(args: dict[str, Any]) -> str | None:
    direct = pick(args, "scene_id", "sceneId", "id")
    if direct:
        return str(direct)
    context = pick(args, "hookContext", "hook_context", default={})
    value = pick(context, "id", "scene_id", "sceneId")
    return str(value) if value else None


def run_bulk(client: StashClient, store: StateStore, force: bool = False) -> dict[str, Any]:
    page = 1
    checked = processed = skipped = failed = 0
    errors: list[dict[str, Any]] = []
    images = entity_images.Enricher(client, store.root)
    while True:
        total, scenes = client.find_scenes(page)
        if not scenes:
            break
        for scene in scenes:
            checked += 1
            try:
                code = extract_code(scene)
                if not code or not needs_processing(scene, code, store, force):
                    skipped += 1
                    continue
                result = process_scene(client, scene, store, force=force, images=images)
                if result.get("ok") and not result.get("skipped"):
                    processed += 1
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                if len(errors) < 30:
                    errors.append({"scene_id": scene.get("id"), "error": str(exc)})
                log(f"scene {scene.get('id')} failed: {exc}")
            time.sleep(0.25)
        if page * PAGE_SIZE >= total:
            break
        page += 1
    return {
        "ok": failed == 0,
        "checked": checked,
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
        "entity_images": images.summary(),
    }


def run_image_bulk(client: StashClient, store: StateStore) -> dict[str, Any]:
    images = entity_images.Enricher(client, store.root)
    scene_errors = []
    unresolved = []
    # Snapshot the stable entity list first, never paginate a shrinking missing filter.
    for kind in ("performer", "studio"):
        page = 1
        while True:
            total, entities = client.find_image_entities(kind, page)
            for entity in entities:
                outcome = images.fill(kind, entity)
                if outcome["status"] in {"unmatched", "failed"} and entity_images.missing_image(entity):
                    unresolved.append((kind, str(entity["id"])))
            if not entities or page * PAGE_SIZE >= total:
                break
            page += 1
    page = 1
    while unresolved:
        total, scenes = client.find_scenes_for_images(page, unresolved)
        for scene in scenes:
            try:
                images.scene(scene, extract_code(scene))
            except Exception as exc:
                if len(scene_errors) < 20:
                    scene_errors.append({"scene_id": scene.get("id"), "error": str(exc)})
        log(f"빈 이미지 관련 장면 {min(page * PAGE_SIZE, total)}/{total}")
        if not scenes or page * PAGE_SIZE >= total:
            break
        page += 1
    result = images.summary()
    result["scene_errors"] = scene_errors
    result["ok"] = result["ok"] and not scene_errors
    log("사진·로고 결과: " + json.dumps(result, ensure_ascii=False))
    return result


def run_name_bulk(client: StashClient, store: StateStore) -> dict[str, Any]:
    """Repair existing performers only; never create actors or alter scenes."""
    resolver = performer_names.Resolver(store.root)
    results: dict[str, dict] = {}
    source_errors = []

    def repair(entity: dict, source: dict | None = None) -> dict:
        identifier = str(entity.get("id") or "")
        previous = results.get(identifier)
        if previous and previous["status"] not in {"unmatched"}:
            return previous
        result = {"id": identifier, "status": "unmatched"}
        try:
            known = entity_images.source_ids(entity, "performer")
            remote_id = next(iter(known)) if len(known) == 1 else ""
            if not known and source and entity_images.matches(entity, source, "performer"):
                remote_id = str(source.get("id") or source.get("remote_site_id") or "")
            if remote_id:
                person = resolver.profile(remote_id)
                source = {"id": remote_id, "name": person["name_ja"]}
                if entity_images.matches(entity, source, "performer"):
                    with entity_images.entity_lock(store.root, "performer", identifier):
                        latest = client.find_image_entity("performer", identifier)
                        if not latest:
                            raise RuntimeError("Stash 배우의 최신 상태를 확인하지 못했습니다.")
                        if entity_images.matches(latest, source, "performer"):
                            before = str(latest.get("name") or "")
                            changed = apply_performer_name(client, latest, person, resolver)
                            after = client.find_image_entity("performer", identifier) if changed else latest
                            if not after:
                                raise RuntimeError("배우 이름 처리 후 상태를 확인하지 못했습니다.")
                            after_name = str(after.get("name") or "")
                            result.update(status="renamed" if normalized(before) != normalized(after_name)
                                          else "aliases" if changed else "unchanged",
                                          before=before, after=after_name, roman=person["name_roman"],
                                          preserved=normalized(after_name) != normalized(person["name_ko"]))
        except Exception as exc:
            result.update(status="failed", error=str(exc))
        results[identifier] = result
        return result

    page = 1
    while True:
        total, scenes = client.find_scenes(page)
        for scene in scenes:
            pending = [p for p in scene.get("performers") or [] if repair(p)["status"] == "unmatched"]
            if not pending:
                continue
            try:
                content = resolver.provider.scene_sources(scene, extract_code(scene))
                for person in pending:
                    candidates = [p for p in content.get("actresses") or []
                                  if entity_images.matches(person, p, "performer")]
                    unique = {str(p["id"]): p for p in candidates}
                    if len(unique) == 1:
                        repair(person, next(iter(unique.values())))
            except Exception as exc:
                if len(source_errors) < 20:
                    source_errors.append({"scene_id": scene.get("id"), "error": str(exc)})
        log(f"배우 이름 정리: 장면 {min(page * PAGE_SIZE, total)}/{total}")
        if not scenes or page * PAGE_SIZE >= total:
            break
        page += 1
    page = 1
    while True:
        total, people = client.find_image_entities("performer", page)
        for person in people:
            repair(person)
        if not people or page * PAGE_SIZE >= total:
            break
        page += 1
    values = list(results.values())
    result = {
        "ok": not source_errors and not any(r["status"] == "failed" for r in values),
        "checked": len(values), "renamed": sum(r["status"] == "renamed" for r in values),
        "aliases_added": sum(r["status"] == "aliases" for r in values),
        "preserved": sum(bool(r.get("preserved")) for r in values),
        "unmatched": sum(r["status"] == "unmatched" for r in values),
        "failed": sum(r["status"] == "failed" for r in values),
        "errors": [r for r in values if r["status"] == "failed"][:20], "source_errors": source_errors,
    }
    log("배우 이름 정리 결과: " + json.dumps(result, ensure_ascii=False))
    return result


def dispatch(request) -> int:
    connection = pick(request, "server_connection", "serverConnection", default={}) or {}
    args = pick(request, "args", default={}) or {}
    root = plugin_dir(connection)
    fanza_core.CACHE_DIR = root / "cache"
    fanza_core.CONFIG.update({
        "add_source_tag": False,
        "fetch_product_page": False,
        "html_supplement": False,
        "stash_relationship_linking": False,
        "graphql_enabled": True,
    })
    store = StateStore(root)
    mode = str(pick(args, "mode", default="hook") or "hook").lower()
    if mode not in {"hook", "single", "bulk", "lookup", "names_bulk", "images_scene", "images_bulk", "subtitle_search", "subtitle_download", "subtitle_rescan", "duplicates_scan", "duplicates_prepare", "duplicates_merge", "images_catalog", "images_candidates", "images_preview", "images_apply", "images_restore", "name_preview", "name_apply", "stashdb_alias_scan", "stashdb_alias_run", "studio_sync_plan", "studio_sync_run", "folder_tags_scene", "folder_tags_bulk"}:
        raise ValueError("알 수 없는 플러그인 작업입니다.")
    force = bool(pick(args, "force", default=False))

    if mode == "lookup":
        code = fanza_core.extract_code_from_text(pick(args, "code", "name"))
        if not code:
            raise RuntimeError("올바른 품번을 입력하세요.")
        emit({"ok": True, "code": code, "metadata": fetch_metadata(code, store, force=force)})
        return 0

    client = StashClient(connection)
    if mode == "folder_tags_bulk":
        emit(folder_tags.run(client, store.root, dry_run=args.get("dry_run") is True, log=log))
        return 0
    if mode == "folder_tags_scene":
        identifier = scene_id_from_args(args)
        if not identifier:
            raise RuntimeError("장면 ID를 찾지 못했습니다.")
        emit(folder_tags.apply_one(client, store.root, identifier))
        return 0
    if mode == "studio_sync_plan":
        emit(studio_sync.plan(client, store.root, args.get("force", False)))
        return 0
    if mode == "studio_sync_run":
        emit(studio_sync.run(client, store.root, args, log))
        return 0
    if mode == "stashdb_alias_scan":
        emit(stashdb_rescrape.scan(client, args))
        return 0
    if mode == "stashdb_alias_run":
        emit(stashdb_rescrape.run(client, store.root, args, log))
        return 0
    if mode == "images_catalog":
        emit(image_upgrade.catalog(client, args.get("kind")))
        return 0
    if mode == "images_candidates":
        emit(image_upgrade.candidates(client, store.root, args.get("kind"), args.get("id")))
        return 0
    if mode in {"images_preview", "images_apply", "images_restore", "name_preview", "name_apply"}:
        handler = {"images_preview": image_upgrade.preview, "images_apply": image_upgrade.apply,
                   "images_restore": image_upgrade.restore, "name_preview": image_upgrade.name_preview,
                   "name_apply": image_upgrade.name_apply}[mode]
        emit(handler(client, store.root, args))
        return 0
    if mode == "duplicates_scan":
        emit(performer_duplicates.scan(client))
        return 0
    if mode == "duplicates_prepare":
        emit(performer_duplicates.prepare(client, args))
        return 0
    if mode == "duplicates_merge":
        emit(performer_duplicates.merge(client, store.root, args))
        return 0
    if mode == "names_bulk":
        emit(run_name_bulk(client, store))
        return 0
    if mode == "images_bulk":
        emit(run_image_bulk(client, store))
        return 0
    if mode == "bulk":
        emit(run_bulk(client, store, force=force))
        return 0

    scene_id = scene_id_from_args(args)
    if not scene_id:
        raise RuntimeError("장면 ID를 찾지 못했습니다.")
    scene = client.find_scene(scene_id)
    if not scene:
        raise RuntimeError(f"Stash 장면 {scene_id}을 찾지 못했습니다.")
    if mode == "images_scene":
        result = entity_images.Enricher(client, store.root).scene(scene, extract_code(scene))
        emit(result)
        return 0
    if mode in {"subtitle_search", "subtitle_download", "subtitle_rescan"}:
        if mode == "subtitle_rescan":
            emit(subtitles.rescan_scene(scene, args, client))
            return 0
        code = extract_code(scene)
        provider = subtitles.SubtitleCat()
        if mode == "subtitle_search":
            emit(subtitles.search_scene(scene, code, provider))
        elif mode == "subtitle_download":
            emit(subtitles.download_scene(scene, code, args, client, provider))
        return 0
    emit(process_scene(client, scene, store, force=force, dry_run=bool(pick(args, "dry_run", default=False)), replace_cover=args.get("replace_cover") is True))
    return 0


def main():
    request = read_input()
    args = pick(request, "args", default={}) or {}
    root = plugin_dir(pick(request, "server_connection", "serverConnection", default={}) or {}) / "data"
    mode = str(args.get("mode") or "hook")
    if mode in {"bulk", "images_bulk", "names_bulk"}:
        with maintenance.operation_lock(root, mode):
            return dispatch(request)
    if mode in {"images_catalog", "images_candidates"}:
        maintenance.clean_previews(root)
    return dispatch(request)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(traceback.format_exc())
        emit(error=str(exc))
        raise SystemExit(1)
