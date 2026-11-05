# -*- coding: utf-8 -*-
"""Repack a PyInstaller one-dir executable, replacing backend.language_assets
with a fuzzy-matching wrapper.

Usage:
    python pyi_patch.py input.exe output.exe [--patch]

The patched lookup keeps exact matching first; when no exact term is found it
matches the longest glossary term contained in the drawing label (ignoring
case/spacing) and replaces only that part, keeping the rest of the label.
"""
import marshal
import struct
import sys
import zlib

ARCHIVE_MAGIC = b"MEI\014\013\012\013\016"
TARGET_MODULE = "backend.language_assets"


def parse_archive(data):
    cookie = data.rfind(ARCHIVE_MAGIC)
    if cookie < 0:
        raise SystemExit("not a pyinstaller archive")
    magic, pkg_len, toc, toc_len, pyver, pylib = struct.unpack("!8sIIii64s", data[cookie : cookie + 88])
    tail = len(data) - cookie - 88
    overlay = len(data) - (pkg_len + tail)
    entries = []
    p = overlay + toc
    end = p + toc_len
    while p < end:
        (size,) = struct.unpack("!i", data[p : p + 4])
        body = data[p + 4 : p + size]
        pos, csize, usize, flag, typ = struct.unpack("!IIIBc", body[:14])
        name = body[14:].decode("utf-8", "replace").rstrip("\0")
        entries.append(
            {
                "name": name,
                "pos": pos,
                "csize": csize,
                "usize": usize,
                "flag": flag,
                "typ": typ,
                "entry": bytearray(data[p : p + size]),
            }
        )
        p += size
    return {
        "data": data,
        "overlay": overlay,
        "pkg_len": pkg_len,
        "toc": toc,
        "toc_len": toc_len,
        "pyver": pyver,
        "pylib": data[cookie + 24 : cookie + 88],
        "entries": entries,
    }


def load_pyz(raw):
    if raw[:4] != b"PYZ\0":
        raise SystemExit("bad PYZ magic")
    magic4 = raw[:4]
    pyc_magic = raw[4:8]
    toc_pos = struct.unpack("!i", raw[8:12])[0]
    order = marshal.loads(raw[toc_pos:])
    modules = {}
    for name, (ispkg, pos, length) in order:
        blob = zlib.decompress(raw[pos : pos + length]) if length else b""
        modules[name] = {"ispkg": ispkg, "blob": blob}
    return magic4, pyc_magic, order, modules


def build_pyz(magic4, pyc_magic, order, modules):
    body = bytearray(magic4 + pyc_magic + b"\0\0\0\0")
    toc = []
    for name, _ in order:
        info = modules[name]
        blob = info["blob"]
        pos = len(body)
        if blob:
            comp = zlib.compress(blob, 6)
            body += comp
            length = len(comp)
        else:
            length = 0
        toc.append((name, (info["ispkg"], pos, length)))
    toc_pos = len(body)
    body[8:12] = struct.pack("!i", toc_pos)
    return bytes(body) + marshal.dumps(toc)


def build_archive(info, new_pyz):
    data = info["data"]
    overlay = info["overlay"]
    payloads = bytearray()
    toc_entries = []
    for e in info["entries"]:
        if e["name"] == "PYZ.pyz":
            payload = new_pyz
            csize = usize = len(payload)
        else:
            payload = data[overlay + e["pos"] : overlay + e["pos"] + e["csize"]]
            csize = e["csize"]
            usize = e["usize"]
        pos = len(payloads)
        payloads += payload
        entry = bytearray(e["entry"])
        struct.pack_into("!III", entry, 4, pos, csize, usize)
        toc_entries.append(bytes(entry))
    toc_blob = b"".join(toc_entries)
    pkg_len = len(payloads) + len(toc_blob) + 88
    cookie = struct.pack(
        "!8sIIii64s",
        ARCHIVE_MAGIC,
        pkg_len,
        len(payloads),
        len(toc_blob),
        info["pyver"],
        info["pylib"],
    )
    return data[:overlay] + bytes(payloads) + toc_blob + cookie


FUZZY_SOURCE = r'''
# --- fuzzy glossary matching (injected) ---
import os as _os
import re as _re
import sys as _sys
import time as _time

_ORIG_CODE = "__ORIG_CODE_PLACEHOLDER__"

_NS = {
    "__name__": __name__,
    "__package__": __package__,
    "__loader__": __loader__,
    "__spec__": __spec__,
    "__doc__": None,
}
exec(_ORIG_CODE, _NS)

globals().update({k: v for k, v in _NS.items() if not k.startswith("__")})

_orig_lookup_term = _NS["LanguageAssets"].lookup_term
_norm = _NS["_normalise"]

_CACHE = {}
_CACHE_TTL = 5.0
_DBG = _os.environ.get("TUYI_FUZZY_DEBUG", "")
_CFG_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(_sys.executable)), "fuzzy_config.json")
_CFG_CACHE = [0.0, True]


def _allow_cjk_leftover():
    now = _time.monotonic()
    if now - _CFG_CACHE[0] < 5.0:
        return _CFG_CACHE[1]
    value = True
    try:
        import json as _json

        with open(_CFG_PATH, encoding="utf-8-sig") as handle:
            data = _json.load(handle)
        if isinstance(data, dict) and "allow_cjk_leftover" in data:
            value = bool(data["allow_cjk_leftover"])
    except Exception:
        value = True
    _CFG_CACHE[0] = now
    _CFG_CACHE[1] = value
    return value


def _has_cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def _log(msg):
    if not _DBG:
        return
    try:
        with open(_DBG, "a", encoding="utf-8") as handle:
            handle.write(msg + "\n")
    except Exception:
        pass


def _terms_for(self, mode, project_path):
    key = (id(self), str(mode or ""), str(project_path or ""))
    now = _time.monotonic()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    try:
        items = _NS["LanguageAssets"].list_terms(self, project_path)
    except Exception as exc:
        _log("list_terms failed: %r" % (exc,))
        items = []
    prepared = []
    for term in items:
        if str(term.get("mode")) != str(mode):
            continue
        term_source = str(term.get("source") or "")
        term_target = str(term.get("target") or "")
        if not term_source or not term_target:
            continue
        norm_source = _norm(term_source)
        if not norm_source:
            continue
        prepared.append(
            (norm_source, term_source, term_target, str(term.get("layer_contains") or "").casefold())
        )
    _CACHE[key] = (now, prepared)
    return prepared


def _fuzzy_lookup(self, source, mode, layer="", project_path=None):
    text = str(source or "")
    if not text.strip():
        return None
    norm_text = _norm(text)
    layer_norm = str(layer or "").casefold()
    best = None
    best_len = 0
    for norm_source, term_source, term_target, rule in _terms_for(self, mode, project_path):
        if rule and rule not in layer_norm:
            continue
        if norm_source == norm_text:
            continue
        if norm_source in norm_text and len(norm_source) > best_len:
            best = (term_source, term_target)
            best_len = len(norm_source)
    if not best:
        return None
    term_source, term_target = best
    parts = [_re.escape(p) for p in term_source.split() if p]
    if not parts:
        return None
    pattern = _re.compile(r"\s*".join(parts), _re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return None
    left = text[: match.start()]
    right = text[match.end() :]
    if not _allow_cjk_leftover() and (_has_cjk(left) or _has_cjk(right)):
        _log("fuzzy %r: %r skipped (cjk leftover)" % (text, term_source))
        return None
    if right and term_target[-1:].isalnum() and (right[0].isalnum() or right[0] in "(["):
        right = " " + right
    if left and term_target[:1].isalnum() and not left.endswith(" ") and (left[-1].isalnum() or left[-1] in ")]"):
        left = left + " "
    result = (left + term_target + right).strip()
    _log("fuzzy %r: %r -> %r" % (text, term_source, result))
    return result or None


def lookup_term(self, source, mode, layer="", project_path=None):
    exact = _orig_lookup_term(self, source, mode, layer, project_path)
    _log("lookup %r mode=%r exact=%r" % (source, mode, exact))
    if exact:
        return exact
    return _fuzzy_lookup(self, source, mode, layer, project_path)


_NS["LanguageAssets"].lookup_term = lookup_term
'''


def make_wrapper(orig_code):
    code = compile(FUZZY_SOURCE, "backend/language_assets.py", "exec")

    def inject(c):
        consts = []
        for const in c.co_consts:
            if const == "__ORIG_CODE_PLACEHOLDER__":
                consts.append(orig_code)
            elif hasattr(const, "co_consts"):
                consts.append(inject(const))
            else:
                consts.append(const)
        return c.replace(co_consts=tuple(consts))

    return inject(code)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    patch = "--patch" in sys.argv
    data = open(src, "rb").read()
    info = parse_archive(data)
    pyz_entry = next(e for e in info["entries"] if e["name"] == "PYZ.pyz")
    start = info["overlay"] + pyz_entry["pos"]
    pyz_raw = data[start : start + pyz_entry["csize"]]
    magic4, pyc_magic, order, modules = load_pyz(pyz_raw)
    print("modules", len(order), "pyz", len(pyz_raw))
    if patch:
        orig_code = marshal.loads(modules[TARGET_MODULE]["blob"])
        modules[TARGET_MODULE]["blob"] = marshal.dumps(make_wrapper(orig_code))
        print("patched", TARGET_MODULE)
    new_pyz = build_pyz(magic4, pyc_magic, order, modules)
    print("new pyz", len(new_pyz))
    out = build_archive(info, new_pyz)
    print("new exe", len(out))
    open(dst, "wb").write(out)


if __name__ == "__main__":
    main()
