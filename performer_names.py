"""Deterministic performer-name transcription; never translate name meanings.

FANZA's nameRuby is the pronunciation authority. Romanized and Hangul forms
are derived from that reading, not advertised as an official English name.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata
import urllib.parse

import entity_images


REVISION = 2
CACHE_SECONDS = 30 * 86400
# Hepburn-style romanization plus a consistent Korean phonetic spelling.
ROWS = (
    ("あいうえお", "a i u e o", "아 이 우 에 오"),
    ("かきくけこ", "ka ki ku ke ko", "카 키 쿠 케 코"),
    ("がぎぐげご", "ga gi gu ge go", "가 기 구 게 고"),
    ("さしすせそ", "sa shi su se so", "사 시 스 세 소"),
    ("ざじずぜぞ", "za ji zu ze zo", "자 지 즈 제 조"),
    ("たちつてと", "ta chi tsu te to", "타 치 츠 테 토"),
    ("だぢづでど", "da ji zu de do", "다 지 즈 데 도"),
    ("なにぬねの", "na ni nu ne no", "나 니 누 네 노"),
    ("はひふへほ", "ha hi fu he ho", "하 히 후 헤 호"),
    ("ばびぶべぼ", "ba bi bu be bo", "바 비 부 베 보"),
    ("ぱぴぷぺぽ", "pa pi pu pe po", "파 피 푸 페 포"),
    ("まみむめも", "ma mi mu me mo", "마 미 무 메 모"),
    ("やゆよ", "ya yu yo", "야 유 요"),
    ("らりるれろ", "ra ri ru re ro", "라 리 루 레 로"),
    ("わゐゑを", "wa wi we o", "와 위 웨 오"),
    ("ぁぃぅぇぉゔ", "a i u e o vu", "아 이 우 에 오 부"),
)
MORAS = {kana: (roman, korean) for letters, romans, koreans in ROWS
         for kana, roman, korean in zip(letters, romans.split(), koreans.split())}
for prefix, roman, korean in (
    ("き", "ky", "캬 큐 쿄"), ("ぎ", "gy", "갸 규 교"),
    ("し", "sh", "샤 슈 쇼"), ("じ", "j", "자 주 조"),
    ("ち", "ch", "차 추 초"), ("ぢ", "j", "자 주 조"),
    ("に", "ny", "냐 뉴 뇨"), ("ひ", "hy", "햐 휴 효"),
    ("び", "by", "뱌 뷰 뵤"), ("ぴ", "py", "퍄 퓨 표"),
    ("み", "my", "먀 뮤 묘"), ("り", "ry", "랴 류 료"),
):
    for small, vowel, syllable in zip("ゃゅょ", "auo", korean.split()):
        MORAS[prefix + small] = (roman + vowel, syllable)
for kana, roman, korean in (
    ("ふぁ", "fa", "파"), ("ふぃ", "fi", "피"), ("ふぇ", "fe", "페"), ("ふぉ", "fo", "포"),
    ("てぃ", "ti", "티"), ("でぃ", "di", "디"), ("とぅ", "tu", "투"), ("どぅ", "du", "두"),
    ("しぇ", "she", "셰"), ("じぇ", "je", "제"), ("ちぇ", "che", "체"),
    ("うぃ", "wi", "위"), ("うぇ", "we", "웨"), ("うぉ", "wo", "워"),
    ("ゔぁ", "va", "바"), ("ゔぃ", "vi", "비"), ("ゔぇ", "ve", "베"), ("ゔぉ", "vo", "보"),
):
    MORAS[kana] = (roman, korean)


def hiragana(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in value)


def roman_key(value: str) -> str:
    value = value.lower().replace("'", "")
    for before, after in (("sya", "sha"), ("syu", "shu"), ("syo", "sho"),
                          ("tya", "cha"), ("tyu", "chu"), ("tyo", "cho"),
                          ("jya", "ja"), ("jyu", "ju"), ("jyo", "jo"),
                          ("zya", "ja"), ("zyu", "ju"), ("zyo", "jo"),
                          ("shi", "si"), ("chi", "ti"), ("tsu", "tu")):
        value = value.replace(before, after)
    return value


def tokenise(reading: str) -> list[tuple[str, str, str]]:
    reading = re.sub(r"[\s・·]+", " ", hiragana(reading))
    if not reading or len(reading) > 100:
        raise ValueError("이름의 읽는 법이 비어 있거나 너무 깁니다.")
    tokens = []
    position = 0
    while position < len(reading):
        char = reading[position]
        if char in " んっー":
            tokens.append((char, {" ": " ", "ん": "n", "っ": "", "ー": ""}[char], ""))
            position += 1
            continue
        kana = reading[position:position + 2]
        if kana not in MORAS:
            kana = char
        if kana not in MORAS:
            raise ValueError("확정할 수 없는 문자가 있는 이름은 뜻으로 번역하지 않습니다.")
        roman, korean = MORAS[kana]
        tokens.append((kana, roman, korean))
        position += len(kana)
    for index, (kana, roman, korean) in enumerate(tokens):
        if kana == "っ":
            following = tokens[index + 1][1] if index + 1 < len(tokens) else ""
            if not following or following[0] in "aeioun ":
                raise ValueError("이름의 촉음 발음을 확정하지 못했습니다.")
            tokens[index] = (kana, "t" if following.startswith("ch") else following[0], "")
        elif kana == "ー":
            previous = tokens[index - 1][1] if index else ""
            if not previous or previous[-1] not in "aeiou":
                raise ValueError("이름의 장음 발음을 확정하지 못했습니다.")
            tokens[index] = (kana, previous[-1], "")
        elif kana == "ん" and index + 1 < len(tokens) and tokens[index + 1][1].startswith(tuple("aeiouy")):
            tokens[index] = (kana, "n'", "")
    return tokens


def join_korean(tokens: list[tuple[str, str, str]]) -> str:
    result = ""
    for kana, _, korean in tokens:
        if kana in {"ん", "っ"}:
            if not result or not "가" <= result[-1] <= "힣" or (ord(result[-1]) - 0xAC00) % 28:
                raise ValueError("이름의 받침을 확정하지 못했습니다.")
            result = result[:-1] + chr(ord(result[-1]) + (4 if kana == "ん" else 19))
        elif kana != "ー":
            result += korean
    return result


def transcribe(reading: str, original: str = "", image_url: str = "") -> dict:
    tokens = tokenise(reading)
    groups = []
    current = []
    for token in tokens:
        if token[0] == " ":
            if current:
                groups.append(current)
                current = []
        else:
            current.append(token)
    if current:
        groups.append(current)
    if len(groups) == 1:
        words = []
        try:
            url = urllib.parse.urlsplit(image_url)
            # A photo filename is only a spacing hint, NEVER a reading source.
            if url.hostname in entity_images.IMAGE_HOSTS and re.search(r"/actjpgs?/", url.path):
                stem = Path(url.path).stem.lower()
                stem = re.sub(r"_\d+$", "", stem)
                if re.fullmatch(r"[a-z]+(?:_[a-z]+){1,3}", stem):
                    words = stem.split("_")
        except ValueError:
            pass
        full = roman_key("".join(t[1] for t in groups[0]))
        if words and "".join(roman_key(w) for w in words) == full:
            split_groups, pending, index = [], [], 0
            for token in groups[0]:
                pending.append(token)
                if index < len(words) and roman_key("".join(t[1] for t in pending)) == roman_key(words[index]):
                    split_groups.append(pending)
                    pending = []
                    index += 1
            if not pending and index == len(words):
                groups = split_groups
        if len(groups) == 1:
            # e.g. 三宮つばき: the written kana suffix confirms the name boundary.
            match = re.fullmatch(r"[㐀-䶿一-鿿々]+([ぁ-ゖァ-ヺー]+)", original)
            if match:
                suffix = hiragana(match[1])
                source = "".join(t[0] for t in groups[0])
                if source.endswith(suffix) and len(source) > len(suffix):
                    boundary = len(source) - len(suffix)
                    for index in range(1, len(groups[0])):
                        if len("".join(t[0] for t in groups[0][:index])) == boundary:
                            groups = [groups[0][:index], groups[0][index:]]
                            break
    return {"reading": hiragana(reading),
            "roman": " ".join("".join(t[1] for t in group).capitalize() for group in groups),
            "korean": " ".join(join_korean(group) for group in groups)}


def transcribe_profile(profile: dict) -> dict:
    """Align current/former names with their own official readings, never meanings."""
    original = unicodedata.normalize("NFKC", str(profile.get("name") or "")).strip()
    reading = unicodedata.normalize("NFKC", str(profile.get("nameRuby") or "")).strip()
    pattern = r"([^()]+)\(([^()]+)\)"
    written, spoken = re.fullmatch(pattern, original), re.fullmatch(pattern, reading)
    if any(c in original + reading for c in "()"):
        if not written or not spoken:
            raise ValueError("현재 이름과 옛 예명의 공식 발음 구성을 확인하지 못했습니다.")
        originals = [written[1].strip(), *[p.strip() for p in re.split(r"[,、]", written[2])]]
        readings = [spoken[1].strip(), *[p.strip() for p in re.split(r"[,、]", spoken[2])]]
        if len(originals) != len(readings) or any(not v for v in originals + readings):
            raise ValueError("이름과 공식 발음의 개수가 맞지 않습니다.")
    else:
        originals, readings = [original], [reading]
    names = [transcribe(r, n, str(profile.get("imageUrl") or "")) for n, r in zip(originals, readings)]
    aliases = list(dict.fromkeys([*originals, *[n["roman"] for n in names], *[n["korean"] for n in names]]))
    return {**names[0], "source_names": originals, "aliases": aliases}


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".performer-name-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Resolver:
    def __init__(self, data_root: Path, provider=None):
        self.root = data_root
        self.provider = provider or entity_images.FanzaProfiles()
        self.memo: dict[str, dict] = {}

    def profile(self, identifier: str) -> dict:
        if not entity_images.numeric_id(identifier):
            raise ValueError("FANZA 배우 식별번호가 없습니다.")
        if identifier in self.memo:
            return self.memo[identifier]
        path = self.root / "performer_names" / "sources" / f"{identifier}.json"
        cached = read_json(path)
        profile = cached.get("profile")
        if (cached.get("revision") != REVISION or not isinstance(profile, dict)
                or not isinstance(cached.get("time"), (int, float))
                or not 0 <= time.time() - cached["time"] < CACHE_SECONDS):
            data = self.provider.query("""query OneClickPerformerName($id: ID!) {
              actress(id: $id) { id name nameRuby imageUrl }
            }""", identifier)
            profile = data.get("actress")
            if not isinstance(profile, dict) or str(profile.get("id")) != identifier:
                raise ValueError("FANZA 배우 식별번호를 확인하지 못했습니다.")
            # Do not let a corrupt/unsupported reading poison the successful cache.
            transcribe_profile(profile)
            write_json(path, {"revision": REVISION, "time": time.time(), "profile": profile})
        if str(profile.get("id")) != identifier:
            raise ValueError("저장된 배우 식별번호가 요청과 다릅니다.")
        name = transcribe_profile(profile)
        result = {"remote_site_id": identifier, "name_ja": profile["name"], "name_ko": name["korean"],
                  "name_roman": name["roman"], "name_reading": name["reading"], "name_method": "fanza-reading-v2",
                  "source_names": name["source_names"], "name_aliases": name["aliases"]}
        self.memo[identifier] = result
        return result

    def legacy_name(self, identifier: str, original: str) -> str:
        if not entity_images.numeric_id(identifier):
            return ""
        data = read_json(self.root / "translations" / f"performer.name-{identifier}.json")
        if data.get("revision") == 2 and data.get("source_sha256") == hashlib.sha256(original.encode("utf-8")).hexdigest():
            return str(data.get("translated") or "")
        return ""

    def applied_name(self, local_id: str, source_id: str) -> str:
        if not entity_images.numeric_id(local_id):
            return ""
        data = read_json(self.root / "performer_names" / "applied" / f"{local_id}.json")
        return str(data.get("name") or "") if data.get("source_id") == source_id and not data.get("manual") else ""

    def mark_applied(self, local_id: str, source_id: str, name: str, manual: bool = False) -> None:
        if not entity_images.numeric_id(local_id) or not entity_images.numeric_id(source_id):
            return
        write_json(self.root / "performer_names" / "applied" / f"{local_id}.json",
                   {"revision": REVISION, "source_id": source_id, "name": name, "time": time.time(), "manual": manual})
