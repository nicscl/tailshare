"""The SVG subset squad and member icons may use, checked and re-serialized (docs/squads.md).

One spec with the Hive app's parser: anything outside it is refused, never cleaned up, so what is stored is
exactly what both sides would draw. The stdlib's expat parses, with DOCTYPE, entities, comments and
processing instructions refused before they can do anything."""

from __future__ import annotations

import math
import re
from xml.parsers import expat

MAX_BYTES = 8 * 1024
MAX_ELEMENTS = 200
MAX_DEPTH = 10
MAX_SEGMENTS = 2000
MAX_NUMBER = 1e6

NS = "http://www.w3.org/2000/svg"
ELEMENTS = {"svg", "g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon"}
GEOMETRY = {"circle": ("cx", "cy", "r"), "ellipse": ("cx", "cy", "rx", "ry"), "rect": ("x", "y", "width", "height", "rx", "ry"),
            "line": ("x1", "y1", "x2", "y2")}
PAINT = ("fill", "stroke")
COMMON = ("transform", "fill", "stroke", "stroke-width", "opacity", "fill-rule", "stroke-linecap", "stroke-linejoin")
DROPPED = {"svg": ("width", "height")}  # the app draws aspect-fit into its own box: accepted, not kept
ATTRS = {"svg": ("viewBox", *COMMON), "g": COMMON, "path": ("d", *COMMON),
         "polyline": ("points", *COMMON), "polygon": ("points", *COMMON),
         **{k: (*v, *COMMON) for k, v in GEOMETRY.items()}}
NAMED = {"black", "white", "red", "green", "blue", "gray", "currentColor", "none"}
ENUMS = {"fill-rule": {"nonzero", "evenodd"}, "stroke-linecap": {"butt", "round", "square"},
         "stroke-linejoin": {"miter", "round", "bevel"}}
TRANSFORMS = {"matrix": (6,), "translate": (1, 2), "scale": (1, 2), "rotate": (1, 3)}
# how many numbers each path command takes per repetition
PATH_ARGS = {"m": 2, "l": 2, "h": 1, "v": 1, "c": 6, "s": 4, "q": 4, "t": 2, "a": 7, "z": 0}

_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_NUM_RE = re.compile(_NUM)
_SEP = re.compile(r"[\s,]*")
_COLOR = re.compile(r"#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}")
# what XML 1.0 allows (Char), less the BOM: anything else, NUL first, could make a parser guess UTF-16 or UCS-4
_NOT_XML = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufefe\uff00-\ufffd\U00010000-\U0010ffff]")


class Bad(ValueError):
    pass


def _number(s: str) -> float:
    if not re.fullmatch(_NUM, s):
        raise Bad(f"not a number: {s[:20]!r}")
    n = float(s)
    if not math.isfinite(n) or abs(n) > MAX_NUMBER:
        raise Bad("number out of range")
    return n


def _fmt(n: float) -> str:
    return str(int(n)) if n == int(n) else repr(round(n, 6)).rstrip("0").rstrip(".")


def _numbers(s: str) -> list[float]:
    s = s.strip()
    if not s:
        return []
    parts = re.split(r"\s*,\s*|\s+", s)
    return [_number(p) for p in parts]


def _path(d: str, budget: list[int]) -> str:
    out, i, n = [], 0, len(d)
    cmd = ""
    while True:
        i = _SEP.match(d, i).end()
        if i >= n:
            break
        if d[i].isalpha():
            cmd = d[i]
            if cmd.lower() not in PATH_ARGS:
                raise Bad(f"path command {cmd!r}")
            i += 1
        elif not cmd or cmd in "zZ":
            raise Bad("path: number without a command")
        if not out and cmd.lower() != "m":
            raise Bad("path must start with M")
        args = []
        for k in range(PATH_ARGS[cmd.lower()]):
            i = _SEP.match(d, i).end()
            if cmd.lower() == "a" and k in (3, 4):  # arc flags: one digit, maybe with no separator after
                if i >= n or d[i] not in "01":
                    raise Bad("arc flags are 0 or 1")
                args.append(d[i])
                i += 1
                continue
            m = _NUM_RE.match(d, i)
            if not m:
                raise Bad("path: expected a number")
            args.append(_fmt(_number(m.group())))
            i = m.end()
        budget[0] += 1
        if budget[0] > MAX_SEGMENTS:
            raise Bad(f"more than {MAX_SEGMENTS} path segments")
        out.append(" ".join([cmd, *args]))
        if cmd in "zZ":
            cmd = ""
        elif cmd in "mM":  # numbers after a moveto are linetos
            cmd = "l" if cmd == "m" else "L"
    if not out:
        raise Bad("empty path")
    return " ".join(out)


def _transform(s: str) -> str:
    out, i = [], 0
    for m in re.finditer(r"\s*([a-z]+)\s*\(([^()]*)\)\s*,?", s):
        if m.start() != i:
            raise Bad("transform")
        i = m.end()
        name, nums = m.group(1), _numbers(m.group(2))
        if name not in TRANSFORMS or len(nums) not in TRANSFORMS[name]:
            raise Bad(f"transform {name!r}")
        out.append(f"{name}({' '.join(map(_fmt, nums))})")
    if i != len(s) or not out:
        raise Bad("transform")
    return " ".join(out)


def _value(el: str, name: str, v: str, budget: list[int]) -> str:
    if name in PAINT:
        if v in NAMED or _COLOR.fullmatch(v):
            return v
        raise Bad(f"{name} colour")
    if name in ENUMS:
        if v in ENUMS[name]:
            return v
        raise Bad(name)
    if name == "d":
        return _path(v, budget)
    if name == "transform":
        return _transform(v)
    if name == "points":
        nums = _numbers(v)
        if len(nums) % 2 or not nums:
            raise Bad("points")
        budget[0] += len(nums) // 2
        if budget[0] > MAX_SEGMENTS:
            raise Bad(f"more than {MAX_SEGMENTS} path segments")
        return " ".join(map(_fmt, nums))
    if name == "viewBox":
        nums = _numbers(v)
        if len(nums) != 4 or nums[2] <= 0 or nums[3] <= 0:
            raise Bad("viewBox")
        return " ".join(map(_fmt, nums))
    if name == "opacity":
        n = _number(v.strip())
        if not 0 <= n <= 1:
            raise Bad("opacity")
        return _fmt(n)
    n = _number(v.strip())  # geometry, width, height, stroke-width
    if n < 0 and name in ("r", "rx", "ry", "width", "height", "stroke-width"):
        raise Bad(f"negative {name}")
    return _fmt(n)


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def clean(text: object) -> str:
    """The icon in canonical form, or Bad. "" stays "" (no icon)."""
    if text is None or text == "":
        return ""
    if not isinstance(text, str):
        raise Bad("icon must be a string")
    if _NOT_XML.search(text):
        raise Bad("character not allowed in XML, or a BOM")
    raw = text.encode()  # no lone surrogates left after the check: valid UTF-8
    if len(raw) > MAX_BYTES:
        raise Bad(f"icon over {MAX_BYTES // 1024} KB")
    out: list[str] = []
    stack: list[tuple[str, int]] = []  # (element, where its start tag is in out)
    count, budget = [0], [0]

    def refuse(what):
        def handler(*_a):
            raise Bad(f"{what} not allowed")
        return handler

    def start(tag, attrs):
        local = tag.split(" ")[-1]  # "ns local" with namespace processing
        if " " in tag and not tag.startswith(NS + " "):
            raise Bad("foreign namespace")
        if local not in ELEMENTS or (local == "svg") != (not stack):
            raise Bad(f"element {local!r}")
        count[0] += 1
        if count[0] > MAX_ELEMENTS:
            raise Bad(f"more than {MAX_ELEMENTS} elements")
        if len(stack) >= MAX_DEPTH:
            raise Bad(f"nested deeper than {MAX_DEPTH}")
        parts = [local] + ([f'xmlns="{NS}"'] if local == "svg" else [])
        for name in sorted(attrs):
            if " " in name:
                raise Bad("namespaced attribute")
            if name in DROPPED.get(local, ()):
                _number(attrs[name].strip().removesuffix("px"))
                continue
            if name not in ATTRS[local]:
                raise Bad(f"attribute {name!r} on {local}")
            parts.append(f'{name}="{_escape(_value(local, name, attrs[name], budget))}"')
        if local == "svg" and "viewBox" not in attrs:
            raise Bad("viewBox is required")
        stack.append((local, len(out)))
        out.append("<" + " ".join(parts) + ">")

    def decl(version, encoding, standalone):  # the app's rule: version 1.0 or 1.1, encoding only "UTF-8" if given
        if version not in ("1.0", "1.1") or encoding not in (None, "UTF-8"):
            raise Bad('only <?xml version="1.0" encoding="UTF-8"?> (1.1, and no encoding, also do)')

    def end(tag):
        local, at = stack.pop()
        if at == len(out) - 1:  # no children
            out[-1] = out[-1][:-1] + "/>"
        else:
            out.append(f"</{local}>")

    def chars(data):
        if data.strip():
            raise Bad("text not allowed")

    p = expat.ParserCreate("UTF-8", namespace_separator=" ")  # whatever the document declares
    p.StartElementHandler, p.EndElementHandler, p.CharacterDataHandler = start, end, chars
    p.XmlDeclHandler = decl
    p.StartDoctypeDeclHandler = refuse("DOCTYPE")
    p.EntityDeclHandler = refuse("entities")
    p.CommentHandler = refuse("comments")
    p.ProcessingInstructionHandler = refuse("processing instructions")
    p.StartCdataSectionHandler = refuse("CDATA")
    p.SkippedEntityHandler = refuse("entities")
    p.ExternalEntityRefHandler = refuse("entities")
    p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        p.Parse(raw, True)
    except expat.ExpatError as e:
        raise Bad(f"not well-formed XML ({expat.ErrorString(e.code)})") from None
    if not out:
        raise Bad("empty icon")
    return "".join(out)
