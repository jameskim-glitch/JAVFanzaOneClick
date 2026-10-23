"""Generic product-code parsing; no vendor-specific CID mapping table.

These are bounded search hints, not authoritative product identifiers.
The metadata layer must validate the identifier returned by FANZA.
"""
import re
import unicodedata

# Accept common filename dash spellings, including the mathematical minus.
_CODE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{1,12})\s*-\s*([0-9]{1,8})([A-Za-z]?)(?![A-Za-z0-9_])")


def normalize_subject_id(raw):
    if raw is None:
        return None
    text = unicodedata.normalize("NFKC", str(raw))
    text = "".join("-" if unicodedata.category(c) == "Pd" or c == "\u2212" else c for c in text)
    found = _CODE.search(text)
    if found is None:
        return None
    return f"{found[1].upper()}-{found[2]}{found[3].upper()}"


def make_cid_candidates(subject_id):
    code = normalize_subject_id(subject_id)
    if code is None:
        return []
    label, serial = code.lower().split("-", 1)
    parsed = re.fullmatch(r"([0-9]+)([a-z]?)", serial)
    digits, ending = parsed.groups()
    number = str(int(digits))
    # Format-only hints, deliberately without maker aliases or numeric prefixes.
    return list(dict.fromkeys(label + value + ending for value in
                              (number.zfill(5), digits, number.zfill(3))))


def make_image_urls(subject_id):
    # These URLs are only fallbacks; prefer the cover URL in metadata responses.
    output = []
    for identifier in make_cid_candidates(subject_id):
        for section in ("digital/video", "mono/movie/adult"):
            output.append(f"https://pics.dmm.co.jp/{section}/{identifier}/{identifier}pl.jpg")
    return output


def matches_discovered_id(subject_id, content_id):
    """Check a server-returned ID without guessing its supplier prefix.

    Never use title substring matching, translate maker aliases, or discard a
    suffix. Different matching IDs must still be treated as ambiguous by callers.
    """
    code = normalize_subject_id(subject_id)
    if code is None or not isinstance(content_id, str):
        return False
    label, serial = code.lower().split('-', 1)
    digits, ending = re.fullmatch(r'([0-9]+)([a-z]?)', serial).groups()
    expected = re.escape(label) + '0*' + str(int(digits)) + re.escape(ending)
    return re.fullmatch(r'(?:h_[0-9]+|[0-9]+)?' + expected, content_id.lower()) is not None


def make_product_urls(subject_id):
    return [f"https://video.dmm.co.jp/av/content/?id={identifier}"
            for identifier in make_cid_candidates(subject_id)]
