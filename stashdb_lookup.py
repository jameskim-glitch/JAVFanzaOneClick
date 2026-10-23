"""StashDB lookup using existing English aliases only; no name translation."""
from __future__ import annotations

import re
import unicodedata
import urllib.parse

import entity_images

ENDPOINT = "https://stashdb.org/graphql"
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
MAX_QUERIES = 12
PROFILE_FIELDS = "disambiguation ethnicity country eye_color height measurements fake_tits career_start career_end tattoos piercings details death_date hair_color weight"


def configured_endpoint(client):
    config = client.graphql("query OneClickHQSources { configuration { general { stashBoxes { endpoint } } } }")
    boxes = ((config.get("configuration") or {}).get("general") or {}).get("stashBoxes") or []
    endpoint = next((b.get("endpoint") for b in boxes if str(b.get("endpoint") or "").rstrip("/").lower() == ENDPOINT), None)
    if not endpoint:
        raise ValueError("Stash 설정에 StashDB 연결이 필요합니다. 개인 API 키를 플러그인에 넣거나 친구에게 공유하지 마세요.")
    return endpoint


def fetch(client, kind, query, endpoint, operation="OneClickHQSearch", full=False):
    field = "scrapeSingle" + kind.capitalize()
    fields = "name aliases remote_site_id urls images birthdate gender" if kind == "performer" else "name aliases remote_site_id urls image"
    if full and kind == "performer":
        fields += " " + PROFILE_FIELDS
    result = client.graphql(f"query {operation}($source: ScraperSourceInput!, $input: ScrapeSingle{kind.capitalize()}Input!) {{ {field}(source: $source, input: $input) {{ {fields} }} }}",
                            {"source": {"stash_box_endpoint": endpoint}, "input": {"query": query}}).get(field)
    # Native Stash returns null (not an empty list) for no scraper results.
    if result is None:
        return []
    if not isinstance(result, list) or any(not isinstance(p, dict) for p in result):
        raise ValueError("StashDB 조회 응답을 확인하지 못했습니다. 다시 조회하세요.")
    return result


def fanza_ids(item, kind):
    ids = entity_images.source_ids(item, kind)
    for text in [item.get("url"), *(item.get("urls") or [])]:
        try:
            url = urllib.parse.urlsplit(str(text or ""))
            if kind == "performer" and url.scheme in {"https", "http"} and url.hostname == "actress.dmm.co.jp" and not url.username and url.port in (None, 80, 443):
                match = re.fullmatch(r"/-/detail/=/actress_id=([0-9]{1,12})/?", url.path)
                if match:
                    ids.add(match[1])
        except ValueError:
            pass
    return ids


def conflict(local, source, kind):
    left, right = fanza_ids(local, kind), fanza_ids(source, kind)
    if left and right and left.isdisjoint(right):
        return "FANZA 식별번호 불일치"
    if kind == "performer":
        for key in ("birthdate", "gender"):
            a, b = str(local.get(key) or ""), str(source.get(key) or "")
            if a and b and a != b and not (key == "birthdate" and (a.startswith(b + "-") or b.startswith(a + "-"))):
                return "생년월일 또는 성별 불일치"
    return ""


def english(value):
    value = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()
    # Existing Latin stage names, not transliteration of Korean labels.
    if (not re.fullmatch(r"[A-Za-zÀ-ž][A-Za-zÀ-ž '\-·.]{1,79}", value)
            or value.casefold() in {"unknown", "unnamed", "n/a", "none"}):
        return ""
    return value


def english_aliases(local):
    return list(dict.fromkeys(v for value in [*(local.get("alias_list") or []), local.get("name")]
                              if (v := english(value))))


def name_key(value):
    text = english(value).casefold()
    words = text.split()
    # Only reverse explicit two-part names. Do not split a joined name by guesswork.
    return " ".join(sorted(words)) if len(words) == 2 else text


def source_names(source):
    values = source.get("aliases") or ""
    return [source.get("name") or "", *(values.split(",") if isinstance(values, str) else values)]


def eligible(local, source, names):
    if conflict(local, source, "performer"):
        return False
    target = {name_key(n) for n in source_names(source)} - {""}
    return bool({name_key(n) for n in names} & target)


def select_name(names, source):
    # Keep StashDB's own first-name/last-name order when it is the same alias.
    canonical = english(source.get("name"))
    if canonical and any(name_key(n) == name_key(canonical) for n in names):
        return canonical
    target = {name_key(n) for n in source_names(source)} - {""}
    return next((n for n in names if name_key(n) in target), "")


def resolve(client, local, endpoint):
    names = english_aliases(local)
    if not names:
        return {"status": "no_english_alias", "reason": "영문 별명 없음"}
    ids = {str(s.get("stash_id") or "").lower() for s in local.get("stash_ids") or []
           if str(s.get("endpoint") or "").rstrip("/").lower() == ENDPOINT}
    if len(ids) > 1 or any(not re.fullmatch(UUID, value) for value in ids):
        return {"status": "conflict", "reason": "StashDB 식별번호 충돌"}
    attempted = []
    if ids:
        identifier = next(iter(ids))
        matches = [p for p in fetch(client, "performer", identifier, endpoint, full=True)
                   if str(p.get("remote_site_id") or "").lower() == identifier]
        if len(matches) != 1 or not eligible(local, matches[0], names):
            return {"status": "conflict", "reason": "기존 StashDB 항목과 영문 별명·프로필 불일치"}
        source = matches[0]
    else:
        queries = []
        for n in sorted(names, key=lambda x: (len(x.split()) != 2, -len(x))):
            for q in ([n, " ".join(reversed(n.split()))] if len(n.split()) == 2 else [n]):
                if q not in queries:
                    queries.append(q)
        if len(queries) > MAX_QUERIES:
            return {"status": "ambiguous", "reason": "영문 별명이 너무 많아 자동 선택하지 않음"}
        matches = {}
        for query in queries:
            attempted.append(query)
            for candidate in fetch(client, "performer", query, endpoint):
                identifier = str(candidate.get("remote_site_id") or "").lower()
                if re.fullmatch(UUID, identifier) and eligible(local, candidate, names):
                    matches[identifier] = candidate
        if len(matches) != 1:
            return {"status": "ambiguous" if matches else "unmatched", "reason": f"일치 후보 {len(matches)}명", "queries": attempted}
        identifier = next(iter(matches))
        # Fetch by exact UUID again; never use the first loose name-search hit.
        fresh = [p for p in fetch(client, "performer", identifier, endpoint, full=True)
                 if str(p.get("remote_site_id") or "").lower() == identifier]
        if len(fresh) != 1 or not eligible(local, fresh[0], names):
            return {"status": "conflict", "reason": "재조회한 배우 정보 불일치", "queries": attempted}
        source = fresh[0]
        # A single short stage-name match needs an ID or birthdate corroboration.
        selected = select_name(names, source)
        if len(selected.split()) == 1 and not (fanza_ids(local, "performer") & fanza_ids(source, "performer")):
            dob = str(local.get("birthdate") or "")
            if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", dob) and dob == source.get("birthdate")):
                return {"status": "ambiguous", "reason": "한 단어 예명은 동일인 추가 근거 필요"}
    return {"status": "matched", "name": select_name(names, source), "source": source,
            "remote_id": str(source["remote_site_id"]).lower(), "queries": attempted}
