"""Download existing Korean SubtitleCat SRTs; never translate or transcribe."""
from __future__ import annotations

import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

ORIGIN = "https://www.subtitlecat.com"
MAX_BYTES = 8 * 1024 * 1024
MAX_RESULTS = 30


def safe_url(value: str, base: str = ORIGIN) -> str:
    url = urllib.parse.urljoin(base, value)
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in {"subtitlecat.com", "www.subtitlecat.com"}
            or parts.username or parts.password or parts.port not in {None, 443}):
        raise ValueError("SubtitleCat HTTPS 주소만 사용할 수 있습니다.")
    path = urllib.parse.unquote(parts.path)
    if "\\" in path or ".." in path.split("/") or any(ord(c) < 32 for c in path):
        raise ValueError("올바르지 않은 자막 주소입니다.")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc,
                                   urllib.parse.quote(path, safe="/"), parts.query, ""))


def code_matches(text: str, code: str) -> bool:
    pieces = []
    for piece in code.upper().split("-"):
        pieces.append("0*" + re.escape(piece.lstrip("0") or "0") if piece.isdigit() else re.escape(piece))
    pattern = r"(?<![A-Z0-9])" + r"[-_.\s]*".join(pieces) + r"(?![A-Z0-9])"
    return bool(re.search(pattern, urllib.parse.unquote(text).upper()))


def page_url(value: str, code: str) -> str:
    url = safe_url(value)
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.unquote(parts.path)
    if parts.query or not re.fullmatch(r"/subs/\d+/[^/]+\.html", path, re.I):
        raise ValueError("자막 상세 페이지 주소가 아닙니다.")
    if not code_matches(Path(path).name, code):
        raise ValueError("현재 영상의 품번과 자막 페이지가 다릅니다.")
    return url


class Links(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.has_search = False
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        item = {k: v or "" for k, v in attrs}
        if tag == "input" and item.get("name") == "search":
            self.has_search = True
        if tag == "a":
            self.current = {"href": item.get("href", ""), "id": item.get("id", ""), "text": ""}

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current is not None:
            self.links.append(self.current)
            self.current = None


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return super().redirect_request(request, fp, code, msg, headers, safe_url(newurl))


class SubtitleCat:
    def __init__(self, fetch: Callable[[str], bytes] | None = None):
        # Injection is only for tests. No plugin argument can override the origin.
        self.fetch = fetch or self._fetch

    @staticmethod
    def _fetch(url: str) -> bytes:
        request = urllib.request.Request(safe_url(url), headers={
            "User-Agent": "Mozilla/5.0 (compatible; JAVFanzaOneClick/1.4.1)",
            "Accept": "text/html,text/plain,*/*",
        })
        try:
            with urllib.request.build_opener(SafeRedirect()).open(request, timeout=20) as response:
                safe_url(response.url)
                raw = response.read(MAX_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"SubtitleCat에 연결하지 못했습니다. 잠시 뒤 다시 시도하세요. ({exc})") from exc
        if len(raw) > MAX_BYTES:
            raise RuntimeError("자막 응답이 너무 커서 다운로드를 중단했습니다.")
        return raw

    def search(self, code: str) -> dict[str, Any]:
        url = ORIGIN + "/index.php?" + urllib.parse.urlencode({"search": code})
        parser = Links(self.fetch(url).decode("utf-8-sig", "replace"))
        if not parser.has_search:
            raise RuntimeError("SubtitleCat 검색 페이지를 읽지 못했습니다. 사이트 차단 또는 형식 변경일 수 있습니다.")
        candidates = []
        seen = set()
        for link in parser.links:
            try:
                target = page_url(link["href"], code)
            except ValueError:
                continue
            if target in seen:
                continue
            seen.add(target)
            candidates.append({"page_url": target, "title": link["text"].strip() or code})
        return {"candidates": candidates[:MAX_RESULTS], "truncated": len(candidates) > MAX_RESULTS,
                "search_url": url}

    def korean_link(self, target: str, code: str) -> str | None:
        target = page_url(target, code)
        source = self.fetch(target).decode("utf-8-sig", "replace")
        if "all language subtitles" not in source.lower():
            raise RuntimeError("자막 상세 페이지를 읽지 못했습니다. 사이트 차단 또는 형식 변경일 수 있습니다.")
        parser = Links(source)
        # A Translate button is deliberately NOT followed. Only a real download link qualifies.
        for link in parser.links:
            if link["id"] != "download_ko" or "download" not in link["text"].lower():
                continue
            url = safe_url(link["href"], target)
            parts = urllib.parse.urlsplit(url)
            path = urllib.parse.unquote(parts.path)
            if (not parts.query and re.fullmatch(r"/subs/\d+/[^/]+[-.]ko\.srt", path, re.I)
                    and code_matches(Path(path).name, code)):
                return url
        return None

    def download(self, target: str, code: str) -> tuple[str, dict[str, Any]] | None:
        url = self.korean_link(target, code)
        if not url:
            return None
        text, stats = validate_srt(self.fetch(url))
        return text, {**stats, "source_url": url, "page_url": page_url(target, code)}


def decode_text(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encodings = ("utf-32",)
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16",)
    else:
        encodings = ("utf-8-sig", "cp949")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RuntimeError("자막의 문자 인코딩을 읽지 못했습니다.")


def validate_srt(raw: bytes, *, korean: bool = True) -> tuple[str, dict[str, Any]]:
    if not raw or len(raw) > MAX_BYTES:
        raise RuntimeError("빈 자막 또는 너무 큰 자막 파일입니다.")
    text = decode_text(raw)
    if "\x00" in text or re.search(r"<!doctype|<html\b|<script\b", text, re.I):
        raise RuntimeError("다운로드 응답이 정상적인 자막 파일이 아닙니다.")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    timing = re.compile(r"^(\d{2,}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2,}:\d{2}:\d{2}[,.]\d{3})[^\n]*$", re.M)

    def seconds(value: str) -> float:
        hh, mm, ss, ms = map(int, re.split(r"[:,.]", value))
        if mm >= 60 or ss >= 60:
            raise RuntimeError("자막 시간 형식이 올바르지 않습니다.")
        return hh * 3600 + mm * 60 + ss + ms / 1000

    count = 0
    last_end = 0.0
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise RuntimeError("SRT 자막 형식이 올바르지 않습니다.")
        match = timing.fullmatch(lines[1])
        if not match or not any(line.strip() for line in lines[2:]):
            raise RuntimeError("자막 시간 또는 대사가 누락되었습니다.")
        start, end = (seconds(value) for value in match.groups())
        if end <= start:
            raise RuntimeError("자막 시작·종료 시간이 올바르지 않습니다.")
        last_end = max(last_end, end)
        count += 1
    if not count or (korean and not re.search(r"[가-힣]", text)):
        raise RuntimeError("한국어 대사가 있는 SRT 자막이 아닙니다.")
    return text, {"cues": count, "last_end_seconds": round(last_end, 3)}


def scene_files(scene: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for file in scene.get("files") or []:
        path = Path(str(file.get("path") or ""))
        if not path.is_absolute() or str(path) in seen:
            continue
        seen.add(str(path))
        existing = []
        other = []
        rescan_paths = []
        for suffix in (".ko.srt", ".ko.vtt", ".kr.srt", ".kr.vtt", ".srt", ".vtt"):
            subtitle = path.with_name(path.stem + suffix)
            if os.path.lexists(subtitle):
                # A valid, unlabelled foreign-language SRT must not block a separate ko track.
                if suffix == ".srt" and subtitle.is_file():
                    try:
                        with subtitle.open("rb") as stream:
                            text, _ = validate_srt(stream.read(MAX_BYTES + 1), korean=False)
                        if not re.search(r"[가-힣]", text):
                            other.append(str(subtitle))
                            continue
                    except (OSError, RuntimeError):
                        pass  # Ambiguous files are retained as a conservative block.
                existing.append(str(subtitle))
                if suffix in {".ko.srt", ".ko.vtt"} and subtitle.is_file():
                    rescan_paths.append(str(subtitle))
        result.append({"index": len(result), "path": str(path), "basename": path.name,
                       "duration": file.get("duration"), "existing": existing,
                       "other": other, "rescan_paths": rescan_paths})
    return result


def save_subtitle(video: Path, text: str) -> tuple[Path, bool]:
    if not video.is_absolute() or not video.is_file():
        raise RuntimeError("Stash의 영상 경로에 접근할 수 없습니다. 영상이 있는 PC에서 실행하세요.")
    destination = video.with_name(video.stem + ".ko.srt")
    if os.path.lexists(destination):
        return destination, False
    # Publish atomically without replacing an existing sidecar (including a symlink).
    fd, temporary = tempfile.mkstemp(prefix=".oneclick-subtitle-", suffix=".tmp", dir=video.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if os.name == "nt":
                # Unlike POSIX rename, Windows rename never replaces an existing target.
                # It is atomic on the same volume and also works on exFAT/NAS volumes.
                os.rename(temporary, destination)
            else:
                os.link(temporary, destination)
        except FileExistsError:
            return destination, False
        return destination, True
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def scan_subtitle(client: Any, path: Path) -> dict[str, Any]:
    try:
        data = client.graphql(
            "mutation OneClickSubtitleScan($input: ScanMetadataInput!) { metadataScan(input: $input) }",
            {"input": {"paths": [str(path)], "rescan": False,
                       "scanGenerateCovers": False, "scanGeneratePreviews": False,
                       "scanGenerateImagePreviews": False, "scanGenerateSprites": False,
                       "scanGeneratePhashes": False, "scanGenerateThumbnails": False}},
        )
        job_id = data.get("metadataScan")
        if not job_id:
            raise RuntimeError("스캔 작업 ID를 받지 못했습니다.")
        return {"scan_job_id": str(job_id)}
    except Exception as exc:
        return {"warning": f"자막은 보존했습니다. 자동 스캔 요청에 실패했습니다. 해당 폴더를 선택 스캔하세요. ({exc})"}


def search_scene(scene: dict[str, Any], code: str, provider: SubtitleCat) -> dict[str, Any]:
    if not code:
        raise RuntimeError("영상의 품번을 찾지 못했습니다. 장면의 품번을 먼저 입력하세요.")
    files = scene_files(scene)
    if not files:
        raise RuntimeError("다운로드할 영상 파일 경로가 없습니다.")
    if all(file["existing"] for file in files):
        return {"ok": True, "status": "existing", "code": code, "files": files, "candidates": []}
    return {"ok": True, "status": "search", "code": code, "files": files, **provider.search(code)}


def selected_file(scene: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    files = scene_files(scene)
    index = args.get("file_index", 0 if len(files) == 1 else None)
    if type(index) is not int or not 0 <= index < len(files):
        raise RuntimeError("적용할 영상 파일을 선택하세요.")
    file = files[index]
    if args.get("file_path") is not None and str(args["file_path"]) != file["path"]:
        raise RuntimeError("검색 후 영상 파일 목록이 바뀌었습니다. 다시 검색하고 적용할 파일을 선택하세요.")
    return file


def rescan_scene(scene: dict[str, Any], args: dict[str, Any], client: Any) -> dict[str, Any]:
    file = selected_file(scene, args)
    paths = file["rescan_paths"]
    if not paths:
        raise RuntimeError("다시 연결할 .ko.srt 또는 .ko.vtt 파일이 없습니다. 한국어 자막 파일명을 확인하세요.")
    results = [scan_subtitle(client, Path(path)) for path in paths]
    return {"ok": True, "status": "rescanned", "path": paths[0], "video_path": file["path"],
            "scan_job_ids": [item["scan_job_id"] for item in results if item.get("scan_job_id")],
            "warning": "\n".join(item["warning"] for item in results if item.get("warning"))}


def download_scene(scene: dict[str, Any], code: str, args: dict[str, Any], client: Any,
                   provider: SubtitleCat) -> dict[str, Any]:
    if not code:
        raise RuntimeError("영상의 품번을 찾지 못했습니다.")
    file = selected_file(scene, args)
    if file["existing"]:
        return {"ok": True, "status": "existing", "code": code, "existing": file["existing"]}
    video = Path(file["path"])
    if not video.is_file():
        raise RuntimeError("영상 파일에 접근할 수 없습니다. 저장 위치와 권한을 확인하세요.")
    target = page_url(str(args.get("page_url") or ""), code)
    downloaded = provider.download(target, code)
    if downloaded is None:
        return {"ok": True, "status": "not_found", "code": code,
                "message": "이 페이지에는 내려받을 수 있는 기존 한국어 자막이 없습니다. 번역·제작하지 않습니다."}
    text, info = downloaded
    # A user may have saved a subtitle while the network request was in flight.
    current = selected_file(scene, args)
    if current["existing"]:
        return {"ok": True, "status": "existing", "code": code, "existing": current["existing"]}
    path, created = save_subtitle(video, text)
    if not created:
        return {"ok": True, "status": "existing", "code": code, "existing": [str(path)]}
    scan = scan_subtitle(client, path)
    warnings = [scan["warning"]] if scan.get("warning") else []
    duration = file.get("duration")
    if isinstance(duration, (int, float)) and duration > 0 and info["last_end_seconds"] > duration + max(30, duration * 0.05):
        warnings.append("자막 시간이 영상 길이보다 깁니다. 분할·편집본 여부와 싱크를 확인하세요.")
    return {"ok": True, "status": "downloaded", "code": code, "path": str(path),
            "video_path": str(video), **info, **scan, "warning": "\n".join(warnings)}
