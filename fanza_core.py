#!/usr/bin/env python3
"""Local FANZA/DMM scene scraper for Stash.

Filename/code extraction, generic CID hints, FANZA's
publicly reachable video GraphQL endpoint as the primary metadata source, and
HTML/cover probing only as fallbacks.

The scraper uses only Python's standard library. Log messages go to stderr and
JSON results go to stdout, as required by Stash script scrapers.
"""
from __future__ import annotations

import gzip
import html as html_lib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPCookieProcessor, ProxyHandler
import http.cookiejar

from jav_fanza_cid import make_cid_candidates, make_image_urls, make_product_urls, normalize_subject_id, matches_discovered_id


def configure_stdio_utf8() -> None:
    """Force Stash pipe I/O to UTF-8 on Windows.

    Windows Python commonly inherits cp949 for stdout/stderr. FANZA metadata and
    error responses contain Japanese characters, so writing either JSON or logs
    can otherwise raise UnicodeEncodeError and make Stash report exit status 1.
    """
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            if stream_name == "stdin":
                reconfigure(encoding="utf-8", errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="backslashreplace", newline="\n")
        except (OSError, ValueError, TypeError):
            # Some embedded Python launchers expose streams that cannot be
            # reconfigured. log() and emit() still have safe fallbacks below.
            pass


configure_stdio_utf8()

VERSION = "1.3.1-oneclick"
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "jav_fanza_config.json"
CACHE_DIR = ROOT / ".jav_fanza_cache_v026"

DEFAULT_CONFIG: dict[str, Any] = {
    "timeout_seconds": 8,
    "cache_hours": 72,
    "debug": False,
    "probe_images": True,
    "fetch_product_page": True,
    "prefer_digital": True,
    "add_source_tag": False,
    "graphql_enabled": True,
    "graphql_url": "https://api.video.dmm.co.jp/graphql",
    "graphql_retries": 2,
    "graphql_limit": 10,
    "html_supplement": False,
    "stash_relationship_linking": False,
    "stash_endpoint": "http://127.0.0.1:9999/graphql",
    "stash_api_key": "",
    "stash_api_key_file": "stash_api_key.txt",
    "stash_config_path": "",
    "stash_timeout_seconds": 6,
    "stash_query_limit": 100,
    "stash_match_aliases": True,
    "stash_create_missing_performers": True,
    "stash_create_missing_studios": True,
    "stash_default_performer_gender": "FEMALE",
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150 Safari/537.36 JAVFanzaLocal/0.2.6",
}

# Hyphenated codes are safest. Compact codes are accepted only when the prefix
# contains at least two letters and the number contains at least two digits.
STRICT_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9_]{1,12})\s*-\s*(\d{2,8}[A-Za-z]?)(?![A-Za-z0-9])")
LOOSE_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,12})[\s._]+(\d{2,8}[A-Za-z]?)(?![A-Za-z0-9])")
COMPACT_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,12})(\d{2,8}[A-Za-z]?)(?![A-Za-z0-9])")
CID_IN_URL_RE = re.compile(r"(?:cid=|[?&]id=)([A-Za-z0-9_]+)", re.I)
DATE_RE = re.compile(r"(?<!\d)(20\d{2})[./-](\d{1,2})[./-](\d{1,2})(?!\d)")


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
    except Exception as exc:
        log(f"Config load failed: {exc}")
    return cfg


CONFIG = load_config()


def log(message: str, *, debug_only: bool = False) -> None:
    if debug_only and not CONFIG.get("debug"):
        return
    text = f"[JAVFanzaLocal {VERSION}] {message}\n"
    try:
        sys.stderr.write(text)
    except UnicodeEncodeError:
        # Last-resort path for non-reconfigurable Windows consoles. Preserve the
        # diagnostic rather than crashing the scraper while trying to log it.
        encoding = getattr(sys.stderr, "encoding", None) or "ascii"
        safe = text.encode(encoding, errors="backslashreplace").decode(encoding, errors="replace")
        sys.stderr.write(safe)
    sys.stderr.flush()


def read_stdin_json() -> Any:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log(f"Invalid JSON input: {exc}")
        return {}


def emit(data: Any) -> None:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    try:
        sys.stdout.write(payload)
    except UnicodeEncodeError:
        # Stash expects UTF-8 JSON. If stdout could not be reconfigured, emit an
        # ASCII-only JSON representation so metadata is not lost.
        sys.stdout.write(json.dumps(data, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.flush()


def normalize_code(prefix: str, number: str) -> str:
    return f"{prefix.upper()}-{number.upper()}"


def extract_code_from_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).translate(str.maketrans({"‐":"-", "‑":"-", "‒":"-", "–":"-", "—":"-", "―":"-", "−":"-"}))
    # Remove extension only; keep all other filename tokens for scanning.
    text = re.sub(r"\.(?:mp4|mkv|avi|wmv|mov|m4v|ts|webm)$", "", text, flags=re.I)
    for regex in (STRICT_CODE_RE, COMPACT_CODE_RE, LOOSE_CODE_RE):
        for match in regex.finditer(text):
            prefix, number = match.group(1), match.group(2)
            # Avoid common codec/resolution tokens.
            if prefix.lower() in {"x", "h", "av", "hevc", "hdr", "uhd"}:
                continue
            candidate = normalize_code(prefix, number)
            if normalize_subject_id(candidate):
                return candidate
    return None


def _walk_strings(value: Any, key: str = "") -> Iterable[tuple[int, str, str]]:
    priority = {
        "code": 0, "filename": 1, "path": 1, "basename": 1,
        "title": 2, "name": 3, "url": 4, "urls": 4,
    }
    if isinstance(value, str):
        yield priority.get(key.lower(), 10), key, value
    elif isinstance(value, dict):
        for child_key, child in value.items():
            yield from _walk_strings(child, str(child_key))
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child, key)


def extract_code_from_fragment(fragment: Any) -> str | None:
    candidates = sorted(_walk_strings(fragment), key=lambda item: item[0])
    for _, _, text in candidates:
        code = extract_code_from_text(text)
        if code:
            return code
    return None


@dataclass
class ParsedHTML:
    title: str = ""
    meta: dict[str, str] = field(default_factory=dict)
    json_ld: list[Any] = field(default_factory=list)
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)


class ProductHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.result = ParsedHTML()
        self._in_title = False
        self._script_type = ""
        self._script_parts: list[str] = []
        self._text_parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = values.get("property") or values.get("name") or values.get("itemprop")
            content = values.get("content")
            if key and content:
                self.result.meta[key.lower()] = content.strip()
        elif tag == "script":
            self._script_type = values.get("type", "").lower()
            self._script_parts = []
        elif tag == "a":
            self._anchor_href = values.get("href", "")
            self._anchor_parts = []
        elif tag in {"br", "p", "div", "li", "tr", "td", "th", "dt", "dd", "h1", "h2", "h3"}:
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            if "ld+json" in self._script_type:
                raw = "".join(self._script_parts).strip()
                if raw:
                    try:
                        self.result.json_ld.append(json.loads(raw))
                    except Exception:
                        pass
            self._script_type = ""
            self._script_parts = []
        elif tag == "a" and self._anchor_href is not None:
            text = " ".join("".join(self._anchor_parts).split())
            if text:
                self.result.links.append((self._anchor_href, text))
            self._anchor_href = None
            self._anchor_parts = []
        elif tag in {"p", "div", "li", "tr", "td", "th", "dt", "dd", "h1", "h2", "h3"}:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.result.title += data
        if self._script_type:
            self._script_parts.append(data)
        else:
            self._text_parts.append(data)
        if self._anchor_href is not None:
            self._anchor_parts.append(data)

    def close(self) -> None:
        super().close()
        self.result.title = " ".join(self.result.title.split())
        text = html_lib.unescape("".join(self._text_parts))
        lines = [" ".join(line.split()) for line in text.splitlines()]
        self.result.text = "\n".join(line for line in lines if line)


class HTTPClient:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self.timeout = int(CONFIG.get("timeout_seconds") or 8)
        self.failed_hosts: set[str] = set()

    def _request(self, url: str, *, range_bytes: bool = False) -> tuple[str, int, dict[str, str], bytes]:
        host = (urlparse(url).hostname or "").lower()
        if host in self.failed_hosts:
            raise URLError(f"host previously failed: {host}")
        headers = {
            "User-Agent": str(CONFIG.get("user_agent")),
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.5",
            "Accept-Encoding": "identity",
            "Cookie": "age_check_done=1; ckcy=1; v=1; dmm_age_check=1",
            "Referer": "https://www.dmm.co.jp/",
        }
        if range_bytes:
            headers["Range"] = "bytes=0-4095"
        request = Request(url, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(2_000_000 if not range_bytes else 4096)
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    body = gzip.decompress(body)
                final_url = response.geturl()
                status = int(getattr(response, "status", 200))
                hdrs = {k.lower(): v for k, v in response.headers.items()}
                return final_url, status, hdrs, body
        except URLError as exc:
            reason = str(getattr(exc, "reason", exc)).lower()
            if host and any(token in reason for token in ("name or service", "getaddrinfo", "nodename", "failed to resolve", "temporary failure in name resolution", "connection refused", "network is unreachable")):
                self.failed_hosts.add(host)
            raise

    def get_html(self, url: str) -> tuple[str, str] | None:
        try:
            final_url, status, headers, body = self._request(url)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            log(f"Page request failed: {url} ({exc})", debug_only=True)
            return None
        ctype = headers.get("content-type", "")
        if status >= 400 or ("html" not in ctype and not body.lstrip().startswith(b"<")):
            return None
        charset = "utf-8"
        match = re.search(r"charset=([\w-]+)", ctype, re.I)
        if match:
            charset = match.group(1)
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        lower = text.lower()
        if "translate_welcome" in final_url or "年齢認証" in text[:5000] or "age check" in lower[:5000]:
            log(f"Age/language gate returned for {url}", debug_only=True)
            return None
        if len(text) < 1500:
            return None
        return final_url, text

    def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        headers = {
            "User-Agent": str(CONFIG.get("user_agent")),
            "Accept": "application/json",
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.5",
            "Content-Type": "application/json",
        }
        raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        retries = max(1, int(CONFIG.get("graphql_retries") or 2))
        for attempt in range(1, retries + 1):
            request = Request(url, data=raw_body, headers=headers, method="POST")
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read(2_000_000)
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                    return data if isinstance(data, dict) else None
            except HTTPError as exc:
                if exc.code == 429 and attempt < retries:
                    time.sleep(min(2 ** attempt, 5))
                    continue
                log(f"GraphQL HTTP error: {exc.code}", debug_only=True)
                return None
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 5))
                    continue
                log(f"GraphQL request failed: {exc}", debug_only=True)
                return None
        return None

    @staticmethod
    def _image_dimensions(body: bytes) -> tuple[int, int] | None:
        if body.startswith(b"\x89PNG") and len(body) >= 24:
            return int.from_bytes(body[16:20], "big"), int.from_bytes(body[20:24], "big")
        if body.startswith(b"\xff\xd8"):
            i = 2
            while i + 9 < len(body):
                if body[i] != 0xFF:
                    i += 1
                    continue
                marker = body[i + 1]
                i += 2
                if marker in {0xD8, 0xD9}:
                    continue
                if i + 2 > len(body):
                    break
                seglen = int.from_bytes(body[i:i + 2], "big")
                if seglen < 2 or i + seglen > len(body):
                    break
                if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and seglen >= 7:
                    height = int.from_bytes(body[i + 3:i + 5], "big")
                    width = int.from_bytes(body[i + 5:i + 7], "big")
                    return width, height
                i += seglen
        return None

    def image_exists(self, url: str) -> bool:
        try:
            _, status, headers, body = self._request(url, range_bytes=True)
        except (HTTPError, URLError, TimeoutError, OSError):
            return False
        ctype = headers.get("content-type", "").lower()
        is_image = ctype.startswith("image/") or body.startswith((b"\xff\xd8\xff", b"\x89PNG", b"RIFF"))
        if status >= 400 or not is_image:
            return False
        dimensions = self._image_dimensions(body)
        if dimensions and dimensions[0] < 300 and dimensions[1] < 300:
            log(f"Rejected small placeholder image {dimensions}: {url}", debug_only=True)
            return False
        return True


HTTP = HTTPClient()


def cache_path(code: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", code)
    return CACHE_DIR / f"{safe}.json"


def read_cache(code: str) -> dict[str, Any] | None:
    path = cache_path(code)
    try:
        max_age = float(CONFIG.get("cache_hours", 72)) * 3600
        if path.exists() and time.time() - path.stat().st_mtime <= max_age:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


def write_cache(code: str, scene: dict[str, Any]) -> None:
    temporary = None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".metadata-", suffix=".tmp", dir=CACHE_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(scene, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, cache_path(code))
    except Exception as exc:
        log(f"Cache write failed: {exc}", debug_only=True)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def clean_title(title: str) -> str:
    value = html_lib.unescape(title or "")
    value = re.sub(r"\s*[-|｜]\s*(?:FANZA|DMM\.com).*?$", "", value, flags=re.I)
    value = re.sub(r"\s*通販.*?$", "", value)
    return " ".join(value.split()).strip()


def normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    match = DATE_RE.search(value)
    if not match:
        return None
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def flatten_json_ld(items: list[Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            found.append(value)
            graph = value.get("@graph")
            if graph is not None:
                visit(graph)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    for item in items:
        visit(item)
    return found


def people_from_json(value: Any) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    values = value if isinstance(value, list) else [value]
    for item in values:
        if isinstance(item, str):
            name, url = item, ""
        elif isinstance(item, dict):
            name = str(item.get("name") or "")
            url = str(item.get("url") or item.get("sameAs") or "")
        else:
            continue
        name = " ".join(name.split())
        if name and not any(x.get("name") == name for x in results):
            entry = {"name": name}
            if url:
                entry["url"] = url
            results.append(entry)
    return results


def parse_label_value(text: str, labels: list[str]) -> str | None:
    # Operates on newline-normalized page text and captures one line after a label.
    escaped = "|".join(re.escape(label) for label in labels)
    patterns = [
        rf"(?:^|\n)(?:{escaped})\s*[：:]?\s*([^\n]+)",
        rf"(?:{escaped})\s*[：:]\s*([^\n]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = " ".join(match.group(1).split()).strip()
            if value:
                return value
    return None


def links_matching(parsed: ParsedHTML, href_patterns: tuple[str, ...]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for href, text in parsed.links:
        low = href.lower()
        if any(pattern in low for pattern in href_patterns):
            name = " ".join(text.split())
            if not name or name in {"詳細", "もっと見る"}:
                continue
            if not any(item["name"] == name for item in out):
                entry = {"name": name}
                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    href = "https://www.dmm.co.jp" + href
                entry["url"] = href
                out.append(entry)
    return out


def parse_product_page(code: str, page_url: str, html: str, fallback_image: str | None) -> dict[str, Any]:
    parser = ProductHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        log(f"HTML parser warning: {exc}", debug_only=True)
    parsed = parser.result
    nodes = flatten_json_ld(parsed.json_ld)

    scene: dict[str, Any] = {"code": code, "urls": [page_url]}
    title = ""
    description = ""
    image: str | None = None
    date: str | None = None
    performers: list[dict[str, str]] = []
    director = ""
    studio: dict[str, str] | None = None
    tags: list[dict[str, str]] = []

    # Prefer Product/Movie JSON-LD nodes, then OpenGraph metadata.
    for node in nodes:
        node_type = node.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if not any(str(t).lower() in {"product", "movie", "videoobject", "creativework"} for t in types if t):
            continue
        title = title or str(node.get("name") or "")
        description = description or str(node.get("description") or "")
        raw_image = node.get("image") or node.get("thumbnailUrl")
        if isinstance(raw_image, list) and raw_image:
            raw_image = raw_image[0]
        if isinstance(raw_image, dict):
            raw_image = raw_image.get("url")
        image = image or (str(raw_image) if raw_image else None)
        date = date or normalize_date(str(node.get("datePublished") or node.get("releaseDate") or ""))
        performers.extend(x for x in people_from_json(node.get("actor") or node.get("actors")) if x not in performers)
        directors = people_from_json(node.get("director"))
        if directors and not director:
            director = directors[0]["name"]
        brand = node.get("brand") or node.get("productionCompany")
        if brand and studio is None:
            if isinstance(brand, dict):
                sname = str(brand.get("name") or "")
                surl = str(brand.get("url") or "")
            else:
                sname, surl = str(brand), ""
            if sname:
                studio = {"name": " ".join(sname.split())}
                if surl:
                    studio["url"] = surl
        genres = node.get("genre") or node.get("keywords")
        if isinstance(genres, str):
            genres = re.split(r"[,、/|]", genres)
        if isinstance(genres, list):
            for genre in genres:
                name = " ".join(str(genre).split())
                if name and not any(t["name"] == name for t in tags):
                    tags.append({"name": name})

    title = title or parsed.meta.get("og:title", "") or parsed.meta.get("twitter:title", "") or parsed.title
    description = description or parsed.meta.get("og:description", "") or parsed.meta.get("description", "")
    image = image or parsed.meta.get("og:image") or parsed.meta.get("twitter:image") or fallback_image
    date = date or normalize_date(parse_label_value(parsed.text, ["配信開始日", "商品発売日", "発売日", "公開日"]))

    if not performers:
        performers = links_matching(parsed, ("article=actress", "/actress/", "actress_id", "performer"))
    if not director:
        director = parse_label_value(parsed.text, ["監督"] ) or ""
    if studio is None:
        studio_name = parse_label_value(parsed.text, ["メーカー", "レーベル", "出版社"])
        if studio_name:
            studio = {"name": studio_name}
    if not tags:
        for item in links_matching(parsed, ("article=keyword", "/genre/", "genre_id", "keyword")):
            tags.append({"name": item["name"]})

    title = clean_title(title)
    if title:
        scene["title"] = title
    if description:
        scene["details"] = " ".join(html_lib.unescape(description).split())
    if image:
        scene["image"] = image
    if date:
        scene["date"] = date
    if performers:
        scene["performers"] = performers
    if director:
        scene["director"] = director
    if studio:
        scene["studio"] = studio
    if CONFIG.get("add_source_tag"):
        if not any(tag["name"] == "FANZA" for tag in tags):
            tags.append({"name": "FANZA"})
    if tags:
        scene["tags"] = tags
    return scene


def canonical_fanza_id(value: str) -> str:
    text = re.sub(r"[^a-z0-9_]", "", str(value or "").lower())
    match = re.match(r"^(.*?)(\d+)([a-z]?)$", text)
    if not match:
        return text
    prefix, digits, suffix = match.groups()
    return f"{prefix}{int(digits)}{suffix}"


def _graphql_query(term: str, limit: int) -> str:
    safe_term = term.replace("\\", "\\\\").replace(chr(34), chr(92) + chr(34))
    lines = [
        "{",
        "  legacySearchPPV(",
        f"    limit: {limit},",
        "    offset: 0,",
        "    sort: SALES_RANK_SCORE,",
        "    floor: AV,",
        f'    queryWord: "{safe_term}"',
        "  ) {",
        "    result {",
        "      contents {",
        "        id",
        "        title",
        "        deliveryStartAt",
        "        contentType",
        "        maker { name }",
        "        actresses { id name }",
        "        packageImage { largeUrl }",
        "        review { average count }",
        "        bookmarkCount",
        "      }",
        "      pageInfo { totalCount }",
        "    }",
        "  }",
        "}",
    ]
    return "\n".join(lines)


def _graphql_content_query() -> str:
    return """query ContentPageData($id: ID!) {
  ppvContent(id: $id) {
    id
    title
    description
    deliveryStartDate
    contentType
    maker { name }
    actresses { id name }
    packageImage { largeUrl }
  }
}"""


def _first_nonempty(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _people_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("items", "nodes", "results", "contents"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        if value.get("name"):
            return [value]
    return []


def scene_from_graphql(code: str, content: dict[str, Any]) -> dict[str, Any]:
    cid = str(_first_nonempty(content, "id", "contentId", "content_id") or "").strip()
    scene: dict[str, Any] = {"code": code}
    title = " ".join(str(_first_nonempty(content, "title", "name") or "").split())
    if title:
        scene["title"] = title
    description = " ".join(str(_first_nonempty(content, "description") or "").split())
    if description:
        scene["details"] = description
    raw_date = _first_nonempty(content, "deliveryStartAt", "deliveryStartDate", "releaseDate", "date")
    date = normalize_date(str(raw_date or ""))
    if date:
        scene["date"] = date

    maker = _first_nonempty(content, "maker", "studio", "label", "brand")
    if isinstance(maker, dict):
        maker_name = " ".join(str(_first_nonempty(maker, "name", "title") or "").split())
    else:
        maker_name = " ".join(str(maker or "").split())
    if maker_name:
        scene["studio"] = {"name": maker_name}

    performers: list[dict[str, Any]] = []
    actresses = _first_nonempty(content, "actresses", "performers", "actors", "cast")
    for actress in _people_list(actresses):
        name = " ".join(str(_first_nonempty(actress, "name", "displayName") or "").split())
        if not name or any(item["name"] == name for item in performers):
            continue
        entry: dict[str, Any] = {"name": name, "gender": "FEMALE"}
        actress_id = str(_first_nonempty(actress, "id", "actressId") or "").strip()
        if actress_id:
            entry["remote_site_id"] = actress_id
            entry["urls"] = [
                f"https://www.dmm.co.jp/digital/videoa/-/list/=/article=actress/id={actress_id}/"
            ]
        performers.append(entry)
    if performers:
        scene["performers"] = performers

    package = _first_nonempty(content, "packageImage", "image", "cover")
    if isinstance(package, dict):
        image = str(_first_nonempty(package, "largeUrl", "url", "imageUrl") or "").strip()
    else:
        image = str(package or "").strip()
    if image:
        if image.startswith("//"):
            image = "https:" + image
        scene["image"] = image
    if cid:
        scene["urls"] = [
            f"https://video.dmm.co.jp/av/content/?id={cid}",
            f"https://www.dmm.co.jp/digital/videoa/-/detail/=/cid={cid}/",
        ]
    tags: list[dict[str, str]] = []
    if CONFIG.get("add_source_tag"):
        tags.append({"name": "FANZA"})
    content_type = str(_first_nonempty(content, "contentType", "type") or "").upper()
    if "VR" in content_type:
        tags.append({"name": "VR"})
    if tags:
        scene["tags"] = tags
    return scene


def merge_scene(primary: dict[str, Any], supplement: dict[str, Any]) -> dict[str, Any]:
    merged = dict(primary)
    for key in ("title", "details", "date", "director", "studio", "image"):
        if not merged.get(key) and supplement.get(key):
            merged[key] = supplement[key]
    for key in ("urls", "tags", "performers"):
        left = merged.get(key) if isinstance(merged.get(key), list) else []
        right = supplement.get(key) if isinstance(supplement.get(key), list) else []
        if not left:
            if right:
                merged[key] = right
            continue
        seen: set[str] = set()
        combined: list[Any] = []
        for item in left + right:
            identity = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if identity not in seen:
                seen.add(identity)
                combined.append(item)
        merged[key] = combined
    return merged


def fetch_fanza_graphql(code: str) -> dict[str, Any] | None:
    if not CONFIG.get("graphql_enabled", True):
        return None
    endpoint = str(CONFIG.get("graphql_url") or "https://api.video.dmm.co.jp/graphql")
    limit = max(1, min(25, int(CONFIG.get("graphql_limit") or 10)))
    terms: list[str] = []
    for value in make_cid_candidates(code):
        if value and value not in terms:
            terms.append(value)

    # Exact content lookup avoids fuzzy-search mismatches and often returns
    # release date, maker and actresses more reliably for recent releases.
    for cid in terms[:8]:
        data = HTTP.post_json(endpoint, {
            "query": _graphql_content_query(),
            "operationName": "ContentPageData",
            "variables": {"id": cid},
        })
        if not data or data.get("errors"):
            continue
        try:
            content = data["data"]["ppvContent"]
        except (KeyError, TypeError):
            continue
        if not isinstance(content, dict):
            continue
        found_id = str(content.get("id") or "").lower()
        if canonical_fanza_id(found_id) != canonical_fanza_id(cid):
            continue
        scene = scene_from_graphql(code, content)
        if scene.get("title") or scene.get("date") or scene.get("performers") or scene.get("studio"):
            log(f"Exact GraphQL metadata found: {found_id} for {code}", debug_only=True)
            return scene
    compact = re.sub(r"[^A-Za-z0-9]", "", code).lower()
    if compact and compact not in terms:
        terms.append(compact)
    if code not in terms:
        terms.append(code)

    all_cids = {value.lower() for value in make_cid_candidates(code)}
    public_canonical = canonical_fanza_id(code)
    # Search the user's code first. Supplier-specific prefixes are discovered
    # from the server response, not copied from a third-party conversion table.
    search_terms = list(dict.fromkeys([code, compact, *terms]))
    for term in search_terms[:8]:
        data = HTTP.post_json(endpoint, {"query": _graphql_query(term, limit)})
        if not data:
            continue
        if data.get("errors"):
            log(f"GraphQL returned errors for {term}: {data.get('errors')}", debug_only=True)
            continue
        try:
            contents = data["data"]["legacySearchPPV"]["result"]["contents"]
        except (KeyError, TypeError):
            log(f"Unexpected GraphQL response shape for {term}", debug_only=True)
            continue
        if not isinstance(contents, list):
            continue
        best: dict[str, Any] | None = None
        best_score = -1
        matching_ids: set[str] = set()
        for item in contents:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "").lower()
            score = 0
            if item_id in all_cids:
                score = 100
            elif item_id == term.lower():
                score = 95
            elif canonical_fanza_id(item_id) == canonical_fanza_id(term):
                score = 90
            elif canonical_fanza_id(item_id) == public_canonical:
                score = 85
            elif matches_discovered_id(code, item_id):
                score = 80
            if score >= 80:
                matching_ids.add(canonical_fanza_id(item_id))
            if score > best_score:
                best_score = score
                best = item
        if best is not None and best_score >= 80 and len(matching_ids) == 1:
            log(f"GraphQL metadata found: {best.get('id')} for {code}", debug_only=True)
            return scene_from_graphql(code, best)
    return None



def _name_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s\u3000]+", "", text)


def _normalise_api_key(raw: Any) -> str:
    """Extract one clean Stash API key from copied text or a scalar value.

    A complete Stash config may contain multiple ``api_key`` fields: the local
    server key at YAML indentation 0 and remote Stash-Box keys inside lists.
    Full config files are therefore handled by ``_api_keys_from_config_text``;
    this helper deliberately normalises only one scalar/snippet.
    """
    text = str(raw or "").lstrip("\ufeff").strip()
    if not text:
        return ""

    # When a short config snippet is pasted manually, prefer an indentation-0
    # local key. Fall back to an indented key only for backwards compatibility.
    config_match = re.search(r"(?mi)^api_key\s*:\s*(.*?)\s*$", text)
    if not config_match:
        config_match = re.search(r"(?mi)^\s+api_key\s*:\s*(.*?)\s*$", text)
    if config_match:
        text = config_match.group(1).strip()

    # Accept values pasted as `ApiKey: ...` or `apikey=...`.
    text = re.sub(r"(?i)^\s*apikey\s*[:=]\s*", "", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()

    # Current Stash API keys are JWT-shaped. Selecting the token removes
    # zero-width characters, labels or surrounding browser text accidentally
    # included while copying from the settings page.
    jwt_tokens = re.findall(
        r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{20,}",
        text,
    )
    if jwt_tokens:
        return max(jwt_tokens, key=len)

    # Legacy keys are opaque strings. Remove only whitespace/control characters;
    # valid Stash keys do not contain whitespace.
    return re.sub(r"\s+", "", text)


def _api_keys_from_config_text(raw: Any) -> list[tuple[str, int, int]]:
    """Return every YAML ``api_key`` scalar as (key, line, indentation).

    Stash's own local API key is a top-level field (indentation 0). Remote
    StashDB/JAVStash entries can also contain an indented ``api_key``. Version
    0.2.7 used the first regex match and could therefore authenticate with the
    wrong remote key. Top-level values are returned first, while all remaining
    values are retained as safe fallbacks.
    """
    text = str(raw or "").lstrip("\ufeff")
    found: list[tuple[str, int, int]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = re.match(r"^([ \t]*)api_key\s*:\s*(.*?)\s*$", line, flags=re.I)
        if not match:
            continue
        indent_text, scalar = match.groups()
        # Strip an unquoted YAML comment. JWT/legacy Stash keys do not contain #.
        scalar = re.sub(r"\s+#.*$", "", scalar).strip()
        key = _normalise_api_key(scalar)
        if not key:
            continue
        indent = len(indent_text.expandtabs(4))
        found.append((key, line_number, indent))

    # The active local server key is top-level. Stable line order is retained
    # within each indentation class for useful diagnostics.
    found.sort(key=lambda item: (item[2] != 0, item[2], item[1]))
    return found



def _powershell_json(script: str) -> Any:
    """Run a small read-only PowerShell query and decode compact JSON."""
    if os.name != "nt":
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for executable in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        try:
            completed = subprocess.run(
                [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=flags,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0 or not completed.stdout:
            continue
        text = completed.stdout.decode("utf-8-sig", errors="replace").strip()
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return None


def _local_stash_process_info() -> dict[str, Any]:
    """Find the Windows process actually listening on the configured Stash port."""
    if os.name != "nt":
        return {}
    try:
        parsed = urlparse(str(CONFIG.get("stash_endpoint") or "http://localhost:9999/graphql"))
        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    except (TypeError, ValueError):
        port = 9999
    script = (
        f"$connection = Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if ($connection) { "
        "$process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $connection.OwningProcess) -ErrorAction SilentlyContinue; "
        "if ($process) { [pscustomobject]@{ ProcessId=$process.ProcessId; Name=$process.Name; ExecutablePath=$process.ExecutablePath; CommandLine=$process.CommandLine } | ConvertTo-Json -Compress } "
        "}"
    )
    data = _powershell_json(script)
    return data if isinstance(data, dict) else {}


def _config_arg_from_command_line(command_line: str) -> str:
    """Extract --config/-c from the Stash Windows command line."""
    text = str(command_line or "")
    patterns = (
        r"(?:^|\s)--config(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))",
        r"(?:^|\s)-c(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return next((group for group in match.groups() if group), "").strip()
    return ""


def _candidate_stash_config_paths() -> tuple[list[Path], dict[str, Any]]:
    """Return likely active config paths in Stash's real precedence order."""
    paths: list[Path] = []

    def add(value: Any) -> None:
        text = str(value or "").strip().strip('\"').strip("'")
        if not text:
            return
        path = Path(os.path.expandvars(os.path.expanduser(text)))
        identity = str(path.resolve()) if path.exists() else str(path)
        for existing in paths:
            other = str(existing.resolve()) if existing.exists() else str(existing)
            if other == identity:
                return
        paths.append(path)

    add(CONFIG.get("stash_config_path"))
    add(os.environ.get("STASH_CONFIG_FILE"))

    process_info = _local_stash_process_info()
    add(_config_arg_from_command_line(str(process_info.get("CommandLine") or "")))

    executable = str(process_info.get("ExecutablePath") or "").strip()
    if executable:
        exe_dir = Path(executable).parent
        add(exe_dir / "config.yml")
        add(exe_dir / "config.yaml")
        add(exe_dir.parent / "config.yml")
        add(exe_dir.parent / "config.yaml")

    add(Path.cwd() / "config.yml")
    add(Path.cwd() / "config.yaml")
    add(ROOT.parent / "config.yml")
    add(ROOT.parent / "config.yaml")

    home = Path.home()
    for base in (home / "Desktop", home / "Downloads", home / "Documents"):
        add(base / "config.yml")
        add(base / "config.yaml")
        if base.exists():
            try:
                for child in list(base.iterdir())[:200]:
                    if child.is_dir():
                        add(child / "config.yml")
                        add(child / "config.yaml")
            except OSError:
                pass

    return paths, process_info


def _read_key_file(path: Path, source: str) -> list[tuple[str, str]]:
    """Read one manual key file or every api_key entry from a YAML config."""
    try:
        if not path.exists() or not path.is_file():
            return []
        text = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() in {".yml", ".yaml"}:
            values = _api_keys_from_config_text(text)
            return [
                (
                    key,
                    f"{source} line {line_number} "
                    f"({'top-level local key' if indent == 0 else f'indented key, {indent} spaces'})",
                )
                for key, line_number, indent in values
            ]
        key = _normalise_api_key(text)
        return [(key, source)] if key else []
    except (OSError, UnicodeError) as exc:
        log(f"Could not read local Stash API key from {path}: {exc}")
    return []


def _local_stash_api_key_candidates() -> list[tuple[str, str]]:
    """Return unique API key candidates, preferring Stash's own config.yml.

    The key stored in the running Stash configuration is the source of truth.
    Manual copies may be stale or may contain invisible browser characters, so
    we try all available sources and remember the first one accepted by Stash.
    """
    candidates: list[tuple[str, str]] = []

    def add(key: Any, source: str) -> None:
        clean = _normalise_api_key(key)
        if clean and all(existing != clean for existing, _ in candidates):
            candidates.append((clean, source))

    # Explicit environment override remains supported.
    add(os.environ.get("STASH_API_KEY"), "STASH_API_KEY environment variable")

    # Test keys from every likely active config. Stash itself gives ./config.yml
    # precedence over ~/.stash/config.yml, so assuming the home file is unsafe.
    config_paths, process_info = _candidate_stash_config_paths()
    if process_info:
        log(
            "Detected local Stash listener: "
            f"pid={process_info.get('ProcessId')}, executable={process_info.get('ExecutablePath') or 'unknown'}, "
            f"command={process_info.get('CommandLine') or 'unknown'}",
            debug_only=True,
        )
    seen_paths: set[str] = set()
    for config_path in config_paths:
        path_key = str(config_path.resolve()) if config_path.exists() else str(config_path)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        items = _read_key_file(config_path, f"local Stash config: {config_path}")
        for key, source in items:
            add(key, source)

    configured_file = str(CONFIG.get("stash_api_key_file") or "stash_api_key.txt").strip()
    if configured_file:
        key_path = Path(os.path.expandvars(os.path.expanduser(configured_file)))
        if not key_path.is_absolute():
            key_path = ROOT / key_path
        items = _read_key_file(key_path, str(key_path))
        for key, source in items:
            add(key, source)

    add(CONFIG.get("stash_api_key"), str(CONFIG_PATH))
    return candidates


def _load_local_stash_api_key() -> tuple[str, str]:
    candidates = _local_stash_api_key_candidates()
    return candidates[0] if candidates else ("", "none")


def _local_stash_endpoint_candidates(configured: str) -> list[str]:
    """Return loopback endpoint variants without changing path/query.

    Windows may route ``localhost`` to IPv6 (::1) while ``127.0.0.1`` reaches
    a distinct IPv4 listener. Older preserved configs used 127.0.0.1, so test
    localhost, IPv4 and IPv6 explicitly before declaring authentication bad.
    """
    raw = str(configured or "http://localhost:9999/graphql").strip()
    values: list[str] = []

    def add(value: str) -> None:
        if value and value not in values:
            values.append(value)

    add(raw)
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").casefold()
        if host in {"localhost", "127.0.0.1", "::1"}:
            port = parts.port
            hosts = ["localhost", "127.0.0.1", "[::1]"]
            for candidate_host in hosts:
                netloc = candidate_host + (f":{port}" if port else "")
                add(urlunsplit((parts.scheme or "http", netloc, parts.path or "/graphql", parts.query, parts.fragment)))
    except (TypeError, ValueError):
        pass
    return values


class LocalStashClient:
    def __init__(self) -> None:
        configured_endpoint = str(CONFIG.get("stash_endpoint") or "http://localhost:9999/graphql")
        self.endpoint_candidates = _local_stash_endpoint_candidates(configured_endpoint)
        self.endpoint = self.endpoint_candidates[0]
        self.api_key_candidates = _local_stash_api_key_candidates()
        self.api_key, self.api_key_source = (
            self.api_key_candidates[0] if self.api_key_candidates else ("", "none")
        )
        self.timeout = max(2, int(CONFIG.get("stash_timeout_seconds") or 6))
        self.query_limit = max(10, min(500, int(CONFIG.get("stash_query_limit") or 100)))
        self._unavailable = False
        self._authenticated_candidate: tuple[str, str] | None = None

    def _candidate_sequence(self) -> list[tuple[str, str]]:
        if self._authenticated_candidate is not None:
            return [self._authenticated_candidate]
        if self.api_key_candidates:
            return self.api_key_candidates
        return [("", "none")]

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any] | None:
        if self._unavailable:
            return None
        body = json.dumps({"query": query, "variables": variables}, ensure_ascii=False).encode("utf-8")
        rejected: list[tuple[str, int]] = []
        connection_errors: list[str] = []

        for base_endpoint in self.endpoint_candidates:
            for api_key, source in self._candidate_sequence():
                # Stash accepts the API key either in the `ApiKey` header or in
                # the documented `apikey` query parameter. Test both transports
                # on localhost, IPv4 and IPv6 loopback variants.
                auth_modes = ["none"] if not api_key else ["header", "query"]
                for auth_mode in auth_modes:
                    headers = {
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": f"JAVFanzaLocal/{VERSION}",
                    }
                    endpoint = base_endpoint
                    if auth_mode == "header":
                        headers["ApiKey"] = api_key
                    elif auth_mode == "query":
                        parts = urlsplit(endpoint)
                        params = parse_qsl(parts.query, keep_blank_values=True)
                        params = [(k, v) for k, v in params if k.casefold() != "apikey"]
                        params.append(("apikey", api_key))
                        endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))

                    request = Request(endpoint, data=body, headers=headers, method="POST")
                    try:
                        with build_opener(ProxyHandler({})).open(request, timeout=self.timeout) as response:
                            payload = json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))
                    except HTTPError as exc:
                        if exc.code == 401:
                            rejected.append((f"{source} via {auth_mode} at {base_endpoint}", len(api_key)))
                            continue
                        connection_errors.append(f"{base_endpoint}: HTTP {exc.code}")
                        continue
                    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                        connection_errors.append(f"{base_endpoint}: {exc}")
                        continue

                    self._authenticated_candidate = (api_key, source)
                    self.api_key, self.api_key_source = api_key, f"{source} via {auth_mode}"
                    self.endpoint = base_endpoint
                    log(f"Local Stash authentication succeeded via {auth_mode} at {base_endpoint}", debug_only=True)
                    if not isinstance(payload, dict):
                        return None
                    if payload.get("errors"):
                        log(f"Local Stash GraphQL error: {payload.get('errors')}")
                        return None
                    data = payload.get("data")
                    return data if isinstance(data, dict) else None

        tried = ", ".join(f"{source} (length {length})" for source, length in rejected) or "no accepted key attempt"
        endpoints = ", ".join(self.endpoint_candidates)
        if rejected:
            log(
                "Local Stash GraphQL authentication failed (HTTP 401) for every key and loopback endpoint candidate. "
                f"Endpoints: {endpoints}. Tried: {tried}."
            )
        else:
            self._unavailable = True
            detail = "; ".join(connection_errors) or "unknown connection error"
            log(f"Local Stash GraphQL unavailable on all loopback endpoint candidates: {detail}")
        return None

    def _find_performer(self, name: str) -> str | None:
        query = """query FindPerformers($q: String!, $perPage: Int!) {
  findPerformers(filter: {q: $q, per_page: $perPage}) {
    performers { id name alias_list }
  }
}"""
        data = self._post(query, {"q": name, "perPage": self.query_limit})
        try:
            items = data["findPerformers"]["performers"] if data else []
        except (KeyError, TypeError):
            items = []
        wanted = _name_key(name)
        exact_alias: str | None = None
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if _name_key(item.get("name")) == wanted:
                return str(item.get("id"))
            if CONFIG.get("stash_match_aliases", True):
                aliases = item.get("alias_list") or []
                if any(_name_key(alias) == wanted for alias in aliases if alias):
                    exact_alias = str(item.get("id"))
        return exact_alias

    def _create_performer(self, performer: dict[str, Any]) -> str | None:
        name = " ".join(str(performer.get("name") or "").split())
        if not name:
            return None
        input_data: dict[str, Any] = {"name": name}
        gender = str(performer.get("gender") or CONFIG.get("stash_default_performer_gender") or "").upper()
        if gender in {"MALE", "FEMALE", "TRANSGENDER_MALE", "TRANSGENDER_FEMALE", "INTERSEX", "NON_BINARY"}:
            input_data["gender"] = gender
        urls = performer.get("urls")
        if isinstance(urls, list) and urls:
            input_data["urls"] = [str(url) for url in urls if url]
        mutation = """mutation CreatePerformer($input: PerformerCreateInput!) {
  performerCreate(input: $input) { id name }
}"""
        data = self._post(mutation, {"input": input_data})
        try:
            return str(data["performerCreate"]["id"]) if data and data.get("performerCreate") else None
        except (KeyError, TypeError):
            return None

    def performer_id(self, performer: dict[str, Any]) -> str | None:
        name = str(performer.get("name") or "").strip()
        if not name:
            return None
        found = self._find_performer(name)
        if found:
            return found
        if not CONFIG.get("stash_create_missing_performers", True):
            return None
        created = self._create_performer(performer)
        if created:
            log(f"Created local Stash performer: {name}", debug_only=True)
        return created

    def _find_studio(self, name: str) -> str | None:
        query = """query FindStudios($q: String!, $perPage: Int!) {
  findStudios(filter: {q: $q, per_page: $perPage}) {
    studios { id name aliases }
  }
}"""
        data = self._post(query, {"q": name, "perPage": self.query_limit})
        try:
            items = data["findStudios"]["studios"] if data else []
        except (KeyError, TypeError):
            items = []
        wanted = _name_key(name)
        exact_alias: str | None = None
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if _name_key(item.get("name")) == wanted:
                return str(item.get("id"))
            if CONFIG.get("stash_match_aliases", True):
                aliases = item.get("aliases") or []
                if any(_name_key(alias) == wanted for alias in aliases if alias):
                    exact_alias = str(item.get("id"))
        return exact_alias

    def _create_studio(self, studio: dict[str, Any]) -> str | None:
        name = " ".join(str(studio.get("name") or "").split())
        if not name:
            return None
        input_data: dict[str, Any] = {"name": name}
        urls = studio.get("urls")
        if isinstance(urls, list) and urls:
            input_data["urls"] = [str(url) for url in urls if url]
        mutation = """mutation CreateStudio($input: StudioCreateInput!) {
  studioCreate(input: $input) { id name }
}"""
        data = self._post(mutation, {"input": input_data})
        try:
            return str(data["studioCreate"]["id"]) if data and data.get("studioCreate") else None
        except (KeyError, TypeError):
            return None

    def studio_id(self, studio: dict[str, Any]) -> str | None:
        name = str(studio.get("name") or "").strip()
        if not name:
            return None
        found = self._find_studio(name)
        if found:
            return found
        if not CONFIG.get("stash_create_missing_studios", True):
            return None
        created = self._create_studio(studio)
        if created:
            log(f"Created local Stash studio: {name}", debug_only=True)
        return created


LOCAL_STASH = LocalStashClient()


def bind_local_relationships(scene: dict[str, Any]) -> dict[str, Any]:
    if not CONFIG.get("stash_relationship_linking", True) or not isinstance(scene, dict):
        return scene
    # Copy only the relationship containers so cached FANZA metadata remains
    # portable and does not persist local Stash IDs.
    result = dict(scene)
    studio = result.get("studio")
    if isinstance(studio, dict) and studio.get("name"):
        studio = dict(studio)
        stored_id = LOCAL_STASH.studio_id(studio)
        if stored_id:
            studio["stored_id"] = stored_id
        result["studio"] = studio

    performers_out: list[dict[str, Any]] = []
    performers = result.get("performers")
    if isinstance(performers, list):
        for performer in performers:
            if not isinstance(performer, dict):
                continue
            entry = dict(performer)
            stored_id = LOCAL_STASH.performer_id(entry)
            if stored_id:
                entry["stored_id"] = stored_id
            performers_out.append(entry)
        if performers_out:
            result["performers"] = performers_out
    return result

def find_image(code: str) -> str | None:
    urls = make_image_urls(code)
    if not urls:
        return None
    if not CONFIG.get("probe_images", True):
        return urls[0]
    for url in urls:
        if HTTP.image_exists(url):
            log(f"Cover found: {url}", debug_only=True)
            return url
    return None


def fetch_metadata(code: str, *, for_query: bool = False, force: bool = False) -> dict[str, Any]:
    if not force:
        cached = read_cache(code)
        if cached:
            return cached

    graphql_scene = fetch_fanza_graphql(code)
    product_urls = make_product_urls(code)
    if CONFIG.get("prefer_digital") is False:
        product_urls.sort(key=lambda u: 0 if "/mono/dvd/" in u else 1)

    if graphql_scene:
        if CONFIG.get("html_supplement", True) and CONFIG.get("fetch_product_page", True):
            for candidate in list(graphql_scene.get("urls") or []) + product_urls:
                page = HTTP.get_html(candidate)
                if not page:
                    continue
                final_url, html = page
                supplement = parse_product_page(code, final_url, html, graphql_scene.get("image"))
                graphql_scene = merge_scene(graphql_scene, supplement)
                if graphql_scene.get("details") or graphql_scene.get("director"):
                    break
        write_cache(code, graphql_scene)
        return graphql_scene

    image = find_image(code)
    if CONFIG.get("fetch_product_page", True):
        for candidate in product_urls:
            page = HTTP.get_html(candidate)
            if not page:
                continue
            final_url, html = page
            cids = make_cid_candidates(code)
            low = html.lower()
            if cids and not any(cid.lower() in low or cid.lower() in final_url.lower() for cid in cids):
                continue
            scene = parse_product_page(code, final_url, html, image)
            if scene.get("title") or scene.get("performers") or scene.get("studio"):
                write_cache(code, scene)
                return scene

    # Cover-only fallback is still useful when both structured metadata and HTML fail.
    scene: dict[str, Any] = {"code": code}
    if for_query:
        scene["title"] = code
    if image:
        scene["image"] = image
    if product_urls:
        scene["urls"] = [product_urls[0]]
    if CONFIG.get("add_source_tag"):
        scene["tags"] = [{"name": "FANZA"}]
    write_cache(code, scene)
    return scene


def handle_query(data: Any) -> list[dict[str, Any]]:
    query = data.get("name") if isinstance(data, dict) else data
    code = extract_code_from_text(query)
    if not code:
        return []
    result = fetch_metadata(code, for_query=True)
    # Query results should always have a visible title.
    result.setdefault("title", code)
    return [bind_local_relationships(result)]


def handle_fragment(data: Any) -> dict[str, Any]:
    code = extract_code_from_fragment(data)
    if not code:
        log("No JAV code found in scene fragment", debug_only=True)
        return {}
    return bind_local_relationships(fetch_metadata(code, for_query=False))


def handle_url(data: Any) -> dict[str, Any]:
    url = data.get("url", "") if isinstance(data, dict) else str(data or "")
    code = extract_code_from_text(url)
    if code:
        return bind_local_relationships(fetch_metadata(code, for_query=False, force=True))
    match = CID_IN_URL_RE.search(url)
    if not match:
        return {}
    page = HTTP.get_html(url)
    if not page:
        return {"urls": [url]}
    final_url, html = page
    # Try to recover the public product code from the page text.
    recovered = extract_code_from_text(html)
    if recovered:
        return bind_local_relationships(parse_product_page(recovered, final_url, html, None))
    parsed = parse_product_page(match.group(1).upper(), final_url, html, None)
    parsed["urls"] = [final_url]
    return bind_local_relationships(parsed)


def run_authcheck() -> int:
    """Verify that the configured local Stash endpoint accepts the API key."""
    client = LocalStashClient()
    data = client._post("query AuthCheck { __typename }", {})
    if data and data.get("__typename") == "Query":
        print(
            f"JAVFanzaLocal {VERSION} local Stash auth: PASS "
            f"({client.endpoint}, key source: {client.api_key_source}, length: {len(client.api_key)})"
        )
        return 0
    process_info = _local_stash_process_info()
    print(
        f"JAVFanzaLocal {VERSION} local Stash auth: FAIL "
        f"({client.endpoint}, key source: {client.api_key_source}, length: {len(client.api_key)})"
    )
    if process_info:
        print(f"Listening process: PID {process_info.get('ProcessId')}, executable={process_info.get('ExecutablePath') or 'unknown'}")
        print(f"Command line: {process_info.get('CommandLine') or 'unknown'}")
    config_paths, _ = _candidate_stash_config_paths()
    existing = [str(path) for path in config_paths if path.exists()]
    print("Existing config candidates tested:")
    for path in existing:
        print(f"  - {path}")
    print("Check the Stash startup log for: using config file: <path>")
    return 1


def run_selftest() -> int:
    expected = {
        "EXAMPLE-007": "example00007",
        "DEMO-120": "demo00120",
        "TEST-00001A": "test00001a",
    }
    failures = []
    for code, wanted in expected.items():
        got = make_cid_candidates(code)
        if not got or got[0] != wanted:
            failures.append(f"{code}: expected {wanted}, got {got[:1]}")

    sample = scene_from_graphql("SONE-655", {
        "id": "sone00655",
        "title": "テスト作品",
        "deliveryStartAt": "2025-05-09",
        "contentType": "TWO_DIMENSION",
        "maker": {"name": "エスワン ナンバーワンスタイル"},
        "actresses": [{"id": "1", "name": "乃坂ひより"}],
        "packageImage": {"largeUrl": "https://pics.dmm.co.jp/digital/video/sone00655/sone00655pl.jpg"},
    })
    if sample.get("title") != "テスト作品" or sample.get("date") != "2025-05-09":
        failures.append(f"GraphQL mapping failed: {sample}")
    sample_performer = (sample.get("performers") or [{}])[0]
    if sample_performer.get("name") != "乃坂ひより" or sample_performer.get("gender") != "FEMALE":
        failures.append(f"GraphQL performer mapping failed: {sample.get('performers')}")
    extraction = {
        r"D:\JAV\[FHD] MISM-450 title.mp4": "MISM-450",
        "SAN476_1080p.mkv": "SAN-476",
        "prefix OLM–344 suffix": "OLM-344",
    }
    for text, wanted in extraction.items():
        got = extract_code_from_text(text)
        if got != wanted:
            failures.append(f"extract {text!r}: expected {wanted}, got {got}")

    sample_html = """
    <html><head>
    <meta property="og:title" content="テスト作品 - FANZA">
    <meta property="og:image" content="https://example.invalid/cover.jpg">
    <script type="application/ld+json">
    {"@type":"Product","name":"テスト作品","description":"説明","datePublished":"2026-07-30","actor":[{"name":"女優A"}],"brand":{"name":"メーカーA"},"genre":["単体作品"]}
    </script></head><body>mism00450</body></html>
    """
    parsed = parse_product_page("MISM-450", "https://example.invalid/mism00450", sample_html, None)
    if parsed.get("title") != "テスト作品" or parsed.get("date") != "2026-07-30":
        failures.append(f"metadata parser: unexpected {parsed}")
    if parsed.get("performers", [{}])[0].get("name") != "女優A":
        failures.append(f"metadata performer parser: unexpected {parsed}")

    if _name_key(" 乃坂 ひより ") != _name_key("乃坂ひより"):
        failures.append("local Stash name normalization failed")
    robust = scene_from_graphql("SONE-655", {
        "id": "sone00655",
        "name": "別フィールド作品",
        "releaseDate": "2026/07/30",
        "studio": {"title": "メーカーB"},
        "performers": {"items": [{"id": "22", "displayName": "女優B"}]},
        "cover": {"url": "https://example.invalid/b.jpg"},
    })
    if robust.get("date") != "2026-07-30" or robust.get("studio", {}).get("name") != "メーカーB":
        failures.append(f"robust GraphQL mapping failed: {robust}")
    if robust.get("performers", [{}])[0].get("name") != "女優B":
        failures.append(f"robust performer mapping failed: {robust}")

    config_sample = """
api_key: local.jwt.token_abcdefghijklmnopqrstuvwxyz
stash_boxes:
  - name: StashDB
    api_key: remote.jwt.token_abcdefghijklmnopqrstuvwxyz
"""
    config_keys = _api_keys_from_config_text(config_sample)
    if len(config_keys) != 2 or config_keys[0][2] != 0 or config_keys[1][2] == 0:
        failures.append(f"config API-key hierarchy parser failed: {config_keys}")
    if _normalise_api_key(config_sample) != "local.jwt.token_abcdefghijklmnopqrstuvwxyz":
        failures.append("top-level API-key preference failed")

    if failures:
        for failure in failures:
            print("FAIL", failure)
        return 1
    print(f"JAVFanzaLocal {VERSION} self-test: PASS ({len(expected) + len(extraction) + 9} checks)")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "fragment"
    if mode == "selftest":
        return run_selftest()
    if mode in {"authcheck", "test-auth", "test_stash"}:
        return run_authcheck()
    data = read_stdin_json()
    try:
        if mode == "query":
            emit(handle_query(data))
        elif mode in {"fragment", "scrape"}:
            emit(handle_fragment(data))
        elif mode in {"url", "scrapeURL"}:
            emit(handle_url(data))
        else:
            log(f"Unknown mode: {mode}")
            emit({})
            return 2
    except Exception as exc:
        log(f"Unhandled error: {type(exc).__name__}: {exc}")
        emit([] if mode == "query" else {})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
