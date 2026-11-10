"""Textual parser for Clang IR (CIR) modules.

Builds a faithful operation tree (module -> functions -> regions -> blocks ->
operations -> nested regions) from the generic MLIR assembly printed by
`clang -Xclang -emit-cir`. Both raw *structured* CIR (cir.scope, cir.if,
cir.ternary, loops) and *flattened* CIR (^bb blocks with cir.br/cir.brcond)
are accepted. Region-carrying operations are never collapsed into one line, so
calls nested in `cir.ternary` regions (short-circuit `&&` / `||`) keep their
own operation records.

The parser is line oriented, which matches the MLIR printer: every operation
starts on its own line, a region opens with a trailing `{`, and a region closes
with a leading `}` that may be followed by the next region (`} else {`,
`}, false {`) or by the remainder of the owning operation (`}) : ... loc(..)`).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

VALUE_RE = re.compile(r"%[A-Za-z0-9_$.\-]+(?:#\d+)?")
SYMBOL_RE = re.compile(r"@(\"(?:[^\"\\]|\\.)*\"|[A-Za-z0-9_$.\-]+)")
RESULTS_RE = re.compile(
    r"^((?:%[A-Za-z0-9_$.\-]+(?::\d+)?)(?:\s*,\s*%[A-Za-z0-9_$.\-]+(?::\d+)?)*)\s*=\s*(.*)$"
)
BLOCK_RE = re.compile(r"^\^([A-Za-z0-9_$.\-]+)\s*(\(.*\))?\s*:$")
LOC_ALIAS_RE = re.compile(r"^(#loc\d*)\s*=\s*loc\((.*)\)\s*$")
ALIAS_RE = re.compile(r"^([!#][A-Za-z0-9_$.\-]+)\s*=\s*(.*)$")

LINKAGE_KEYWORDS = (
    "internal", "cir_private", "linkonce_odr", "linkonce", "weak_odr", "weak",
    "available_externally", "common", "appending", "extern_weak", "external",
)
STATIC_LINKAGES = ("internal", "private", "cir_private")

_OPEN = {"(": ")", "[": "]", "{": "}", "<": ">"}
_CLOSE = {v: k for k, v in _OPEN.items()}


# -----------------------------------------------------------------------------
# Low-level text helpers
# -----------------------------------------------------------------------------
def mask_strings(text: str) -> str:
    """Replace string-literal contents with '_' so brackets inside never count."""
    out = []
    in_str = False
    escaped = False
    for ch in text:
        if in_str:
            if escaped:
                escaped = False
                out.append("_")
            elif ch == "\\":
                escaped = True
                out.append("_")
            elif ch == '"':
                in_str = False
                out.append(ch)
            else:
                out.append("_")
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
    return "".join(out)


def strip_comment(line: str) -> str:
    masked = mask_strings(line)
    idx = masked.find("//")
    return line if idx < 0 else line[:idx]


def find_matching(text: str, start: int, masked: Optional[str] = None) -> int:
    """Index of the bracket closing text[start], or -1. Ignores '->' arrows."""
    masked = masked if masked is not None else mask_strings(text)
    depth = []
    i = start
    while i < len(masked):
        ch = masked[i]
        if ch == ">" and i > 0 and masked[i - 1] == "-":
            i += 1
            continue
        if ch in _OPEN:
            depth.append(_OPEN[ch])
        elif ch in _CLOSE:
            if not depth or depth[-1] != ch:
                return -1
            depth.pop()
            if not depth:
                return i
        i += 1
    return -1


def split_top_level(text: str, sep: str = ",") -> List[str]:
    """Split on `sep` outside (), [], {}, <> and string literals."""
    masked = mask_strings(text)
    parts: List[str] = []
    depth = 0
    last = 0
    i = 0
    while i < len(masked):
        ch = masked[i]
        if ch == ">" and i > 0 and masked[i - 1] == "-":
            i += 1
            continue
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        elif depth == 0 and masked.startswith(sep, i):
            parts.append(text[last:i])
            last = i + len(sep)
            i += len(sep)
            continue
        i += 1
    parts.append(text[last:])
    return [p.strip() for p in parts if p.strip()]


def rfind_top_level(text: str, token: str) -> int:
    """Last index of `token` at bracket depth 0, or -1."""
    masked = mask_strings(text)
    depth = 0
    found = -1
    i = 0
    while i < len(masked):
        ch = masked[i]
        if masked.startswith(token, i) and depth == 0:
            found = i
            i += len(token)
            continue
        if ch == ">" and i > 0 and masked[i - 1] == "-":
            i += 1
            continue
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        i += 1
    return found


def _loc_spans(text: str) -> List[Tuple[int, int]]:
    """(start, end) spans of every top-level-or-nested `loc(...)` group."""
    masked = mask_strings(text)
    spans = []
    for match in re.finditer(r"(?<![A-Za-z0-9_])loc\(", masked):
        open_idx = match.end() - 1
        close_idx = find_matching(text, open_idx, masked)
        if close_idx > 0:
            spans.append((match.start(), close_idx + 1))
    return spans


def last_loc(text: str) -> Optional[str]:
    """Body of the last outermost `loc(...)` group in `text`."""
    spans = _loc_spans(text)
    if not spans:
        return None
    start, end = spans[-1]
    # Skip a nested loc (e.g. inside fused[...]) if it is contained in another.
    for s, e in reversed(spans):
        if not any(s2 < s and e <= e2 for s2, e2 in spans):
            start, end = s, e
            break
    return text[start + 4:end - 1].strip()


def strip_locs(text: str) -> str:
    spans = _loc_spans(text)
    out = []
    last = 0
    for s, e in spans:
        if s < last:
            continue  # nested inside an already removed span
        out.append(text[last:s])
        last = e
    out.append(text[last:])
    text = "".join(out)
    # Normalize printer spacing, but preserve the bytes inside string literals.
    # Constants, symbol names and source paths may contain repeated spaces.
    return _normalize_spacing(text)


def _normalize_spacing(text: str) -> str:
    masked = mask_strings(text)
    out = []
    last = 0
    for match in re.finditer(r"\s+", masked):
        out.extend((text[last:match.start()], " "))
        last = match.end()
    out.append(text[last:])
    return "".join(out).strip()


# -----------------------------------------------------------------------------
# Locations
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class SourcePoint:
    file: str
    line: int
    col: int


class LocationTable:
    """Resolves MLIR location aliases and inline locations to source points."""

    def __init__(self, raw: Dict[str, str]):
        self.raw = raw
        self._cache: Dict[str, Tuple[SourcePoint, ...]] = {}

    def resolve(self, body: Optional[str], depth: int = 0) -> Tuple[SourcePoint, ...]:
        if not body or depth > 32:
            return ()
        body = body.strip()
        if body in self._cache:
            return self._cache[body]
        points: Tuple[SourcePoint, ...] = ()
        simple = re.match(r'^"((?:[^"\\]|\\.)*)":(\d+):(\d+)(?:\s+to\s+(\d*):?(\d+))?$', body)
        if simple:
            file = simple.group(1)
            line, col = int(simple.group(2)), int(simple.group(3))
            pts = [SourcePoint(file, line, col)] if line > 0 else []
            if simple.group(5) and line > 0:
                end_line = int(simple.group(4)) if simple.group(4) else line
                pts.append(SourcePoint(file, end_line, int(simple.group(5))))
            points = tuple(pts)
        elif body in self.raw:
            points = self.resolve(self.raw[body], depth + 1)
        elif body.startswith("fused"):
            bracket = body.find("[")
            if bracket >= 0:
                close = find_matching(body, bracket)
                inner = body[bracket + 1:close if close > 0 else len(body)]
                acc: List[SourcePoint] = []
                for part in split_top_level(inner):
                    acc.extend(self.resolve(part, depth + 1))
                points = tuple(acc)
        elif body.startswith("callsite("):
            inner = body[len("callsite("):-1]
            callee = split_top_level(inner, " at ")
            points = self.resolve(callee[0] if callee else None, depth + 1)
        elif body.startswith('"'):
            named = re.match(r'^"(?:[^"\\]|\\.)*"\((.*)\)$', body)
            if named:
                points = self.resolve(named.group(1), depth + 1)
        elif body.startswith("loc("):
            points = self.resolve(body[4:-1], depth + 1)
        self._cache[body] = points
        return points


# -----------------------------------------------------------------------------
# Raw operation tree
# -----------------------------------------------------------------------------
@dataclass
class RawBlock:
    label: Optional[str]
    args: List[Tuple[str, str]] = field(default_factory=list)  # (name, type)
    ops: List["RawOp"] = field(default_factory=list)


@dataclass
class RawRegion:
    label: Optional[str] = None
    blocks: List[RawBlock] = field(default_factory=lambda: [RawBlock(None)])


@dataclass
class Successor:
    label: str
    operands: List[str]
    caseValue: Optional[str] = None


@dataclass
class RawOp:
    name: str
    results: List[str]
    header: str                     # op text after "results =", up to its first region
    operands: List[str]
    successors: List[Successor]
    symbol: Optional[str]           # first @symbol reference in the header
    lineNo: int
    regions: List[RawRegion] = field(default_factory=list)
    trailers: List[str] = field(default_factory=list)
    loc: Optional[str] = None

    @property
    def full_text(self) -> str:
        """Operation spelling with nested regions elided as '{…}'."""
        text = self.header
        for trailer in self.trailers:
            text = text + "…}" + (" " if trailer[:1].isalpha() else "") + trailer
        prefix = ", ".join(self.results) + " = " if self.results else ""
        return prefix + text

    @property
    def display_text(self) -> str:
        return strip_locs(self.full_text)


@dataclass
class RawFunction:
    symbol: str
    keywords: List[str]
    params: List[Tuple[Optional[str], str]]  # (SSA name or None for declarations, type)
    resultTypes: List[str]
    header: str
    lineNo: int
    body: Optional[RawRegion] = None
    loc: Optional[str] = None

    @property
    def is_declaration(self) -> bool:
        return self.body is None

    @property
    def linkage(self) -> str:
        return linkage_from_keywords(self.keywords, self.is_declaration)


@dataclass
class RawGlobal:
    symbol: str
    keywords: List[str]
    type: Optional[str]
    initializer: Optional[str]
    lineNo: int
    loc: Optional[str] = None

    @property
    def is_declaration(self) -> bool:
        return self.initializer is None and "ctor" not in self.keywords

    @property
    def linkage(self) -> str:
        return linkage_from_keywords(self.keywords, self.is_declaration)

    @property
    def is_constant(self) -> bool:
        return "constant" in self.keywords


@dataclass
class RawModule:
    path: str
    sourcePath: str
    contentHash: str
    locations: LocationTable
    typeAliases: Dict[str, str]
    attrAliases: Dict[str, str]
    functions: List[RawFunction]
    globals: List[RawGlobal]
    isFlat: bool = False

    def function(self, symbol: str) -> Optional[RawFunction]:
        for fn in self.functions:
            if fn.symbol == symbol:
                return fn
        return None


def linkage_from_keywords(keywords: List[str], is_declaration: bool) -> str:
    for word in LINKAGE_KEYWORDS:
        if word in keywords:
            return word
    # `private` is printed as MLIR symbol visibility; twice means private linkage.
    if not is_declaration and keywords.count("private") >= 2:
        return "private"
    return "external"


def _strip_symbol(sym: str) -> str:
    if sym.startswith('"') and sym.endswith('"'):
        return sym[1:-1]
    return sym


# -----------------------------------------------------------------------------
# Line-level parsing
# -----------------------------------------------------------------------------
def _logical_lines(text: str) -> List[Tuple[int, str]]:
    """Join physical lines that continue an unbalanced (...) or [...] group."""
    out: List[Tuple[int, str]] = []
    buffer = ""
    start_line = 0
    balance = 0
    for number, physical in enumerate(text.splitlines(), 1):
        line = strip_comment(physical).strip()
        if not line:
            continue
        if not buffer:
            start_line = number
        buffer = f"{buffer} {line}" if buffer else line
        masked = mask_strings(line)
        masked_no_arrow = masked.replace("->", "")
        balance += masked_no_arrow.count("(") + masked_no_arrow.count("[")
        balance -= masked_no_arrow.count(")") + masked_no_arrow.count("]")
        opens_region = masked.rstrip().endswith("{")
        closes_region = masked.lstrip().startswith("}")
        if balance <= 0 or opens_region or closes_region:
            out.append((start_line, buffer))
            buffer = ""
            balance = 0
    if buffer:
        out.append((start_line, buffer))
    return out


def _region_label(text: str) -> Optional[str]:
    """Keyword naming a region about to open: 'else', 'false', 'do', 'body'..."""
    before = text.rstrip()
    if before.endswith("{"):
        before = before[:-1].rstrip()
    match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)$", before)
    if match and not before.endswith(")"):
        word = match.group(1)
        if not word.startswith("cir") and "%" not in word:
            return word
    return None


def _trailer_region_args(text: str) -> List[Tuple[str, str]]:
    """Entry arguments of a region reopened by a trailer such as
    `} catch all (%eh_token : !cir.eh_token) {`."""
    body = text.rstrip()
    if not body.endswith("{"):
        return []
    body = body[:-1].rstrip()
    if not body.endswith(")"):
        return []
    masked = mask_strings(body)
    depth = 0
    for start in range(len(masked) - 1, -1, -1):
        if masked[start] == ")":
            depth += 1
        elif masked[start] == "(":
            depth -= 1
            if depth == 0:
                break
    else:
        return []
    args = []
    for part in split_top_level(strip_locs(body[start + 1:-1])):
        match = re.match(r"^(%[A-Za-z0-9_$.\-]+)\s*:\s*(.+)$", part)
        if not match:
            return []
        args.append((match.group(1), match.group(2).strip()))
    return args


def _parse_successors(header: str) -> Tuple[List[Successor], str]:
    """Extract ^bb successors (with operands) and return header without them."""
    masked = mask_strings(header)
    successors: List[Successor] = []
    removed: List[Tuple[int, int]] = []
    for match in re.finditer(r"\^([A-Za-z0-9_$.\-]+)", masked):
        start, end = match.start(), match.end()
        operands: List[str] = []
        if end < len(masked) and masked[end] == "(":
            close = find_matching(header, end, masked)
            if close > 0:
                operands = VALUE_RE.findall(masked[end:close + 1])
                end = close + 1
        case_value = None
        prefix = masked[:start].rstrip()
        case_match = re.search(r"(-?\d+)\s*:$", prefix)
        if case_match:
            case_value = case_match.group(1)
        successors.append(Successor(match.group(1), operands, case_value))
        removed.append((start, end))
    if not removed:
        return successors, header
    pieces = []
    last = 0
    for s, e in removed:
        pieces.append(header[last:s])
        last = e
    pieces.append(header[last:])
    return successors, "".join(pieces)


def _expand_results(spec: str) -> List[str]:
    results: List[str] = []
    for part in split_top_level(spec):
        match = re.match(r"^(%[A-Za-z0-9_$.\-]+)(?::(\d+))?$", part.strip())
        if not match:
            continue
        if match.group(2):
            results.extend(f"{match.group(1)}#{i}" for i in range(int(match.group(2))))
        else:
            results.append(match.group(1))
    return results


def parse_operation(text: str, line_no: int) -> RawOp:
    results: List[str] = []
    body = text.strip()
    match = RESULTS_RE.match(body)
    if match:
        results = _expand_results(match.group(1))
        body = match.group(2).strip()
    name_match = re.match(r'^"([^"]+)"|^([A-Za-z_][A-Za-z0-9_.$]*)', body)
    name = (name_match.group(1) or name_match.group(2)) if name_match else "unknown"

    header_for_operands = body
    if mask_strings(header_for_operands).rstrip().endswith("{"):
        header_for_operands = header_for_operands.rstrip()[:-1]
    without_locs = strip_locs(header_for_operands)
    successors, without_succ = _parse_successors(without_locs)
    operands = VALUE_RE.findall(mask_strings(without_succ))
    symbol_match = SYMBOL_RE.search(without_locs)
    symbol = _strip_symbol(symbol_match.group(1)) if symbol_match else None
    op = RawOp(name=name, results=results, header=body, operands=operands,
               successors=successors, symbol=symbol, lineNo=line_no)
    if not mask_strings(body).rstrip().endswith("{"):
        op.loc = last_loc(body)
    return op


def _parse_function_header(text: str, line_no: int) -> RawFunction:
    masked = mask_strings(text)
    at = masked.find("@")
    keywords = masked[len("cir.func"):at].split() if at > 0 else []
    sym_match = SYMBOL_RE.search(text, at if at >= 0 else 0)
    symbol = _strip_symbol(sym_match.group(1)) if sym_match else f"<anon@{line_no}>"
    params: List[Tuple[Optional[str], str]] = []
    result_types: List[str] = []
    rest = text[sym_match.end():] if sym_match else ""
    if rest.startswith("("):
        close = find_matching(rest, 0)
        arg_text = rest[1:close]
        for arg in split_top_level(arg_text):
            arg = strip_locs(arg)
            if arg == "...":
                params.append((None, "..."))
                continue
            arg_match = re.match(r"^(%[A-Za-z0-9_$.\-]+)\s*:\s*(.*)$", arg)
            name, ptype = (arg_match.group(1), arg_match.group(2)) if arg_match else (None, arg)
            params.append((name, _strip_attr_dict(ptype)))
        rest = rest[close + 1:]
    arrow = re.match(r"^\s*->\s*", rest)
    if arrow:
        rest = rest[arrow.end():]
        if rest.startswith("("):
            close = find_matching(rest, 0)
            result_types = [_strip_attr_dict(t) for t in split_top_level(rest[1:close])]
        else:
            # A single type can contain spaces inside nested <> groups.
            # Stop only at a top-level suffix (attributes, location or body).
            depth = 0
            masked_rest = mask_strings(rest)
            end = len(rest)
            for index, ch in enumerate(masked_rest):
                if ch.isspace() and depth == 0:
                    end = index
                    break
                if ch == ">" and index > 0 and masked_rest[index - 1] == "-":
                    continue
                if ch in "(<[":
                    depth += 1
                elif ch in ")>]":
                    depth -= 1
            result_types = [_strip_attr_dict(rest[:end])]
    return RawFunction(symbol=symbol, keywords=keywords, params=params,
                       resultTypes=result_types, header=text, lineNo=line_no)


def _strip_attr_dict(type_text: str) -> str:
    """Drop trailing `{llvm.noundef}`-style argument attribute dictionaries."""
    text = strip_locs(type_text).strip()
    masked = mask_strings(text)
    depth = 0
    for i, ch in enumerate(masked):
        if ch == ">" and i > 0 and masked[i - 1] == "-":
            continue
        if ch == "{" and depth == 0:
            return text[:i].strip()
        if ch in "(<[":
            depth += 1
        elif ch in ")>]":
            depth -= 1
    return text


def _parse_global(text: str, line_no: int) -> RawGlobal:
    masked = mask_strings(text)
    at = masked.find("@")
    keywords = [k for k in masked[len("cir.global"):at].split() if not k.startswith('"')]
    sym_match = SYMBOL_RE.search(text, at if at >= 0 else 0)
    symbol = _strip_symbol(sym_match.group(1)) if sym_match else f"<anon@{line_no}>"
    rest = strip_locs(text[sym_match.end():] if sym_match else "")
    if rest.rstrip().endswith("{"):
        rest = rest.rstrip()[:-1]
    initializer = None
    gtype = None
    rest = rest.strip()
    if rest.startswith("="):
        rest = rest[1:].strip()
        colon = rfind_top_level(rest, " : ")
        if colon >= 0:
            initializer = rest[:colon].strip()
            gtype = _strip_attr_dict(rest[colon + 3:])
        else:
            initializer = rest
    elif rest.startswith(":"):
        gtype = _strip_attr_dict(rest[1:])
    if initializer is not None and initializer.split()[0:1] == ["ctor"]:
        keywords.append("ctor")
    return RawGlobal(symbol=symbol, keywords=keywords, type=gtype,
                     initializer=initializer, lineNo=line_no, loc=last_loc(text))


@dataclass
class _Frame:
    kind: str  # root | module | func | op | opaque
    obj: object = None
    region: Optional[RawRegion] = None

    @property
    def block(self) -> RawBlock:
        return self.region.blocks[-1]


def parse_cir_text(text: str, path: str = "<memory>") -> RawModule:
    loc_raw: Dict[str, str] = {}
    type_aliases: Dict[str, str] = {}
    attr_aliases: Dict[str, str] = {}
    functions: List[RawFunction] = []
    globals_: List[RawGlobal] = []
    module_source = None
    is_flat = False

    stack: List[_Frame] = [_Frame("root")]

    for line_no, line in _logical_lines(text):
        masked = mask_strings(line)
        top = stack[-1]

        if top.kind == "root":
            loc_match = LOC_ALIAS_RE.match(line)
            if loc_match:
                loc_raw[loc_match.group(1)] = loc_match.group(2).strip()
                continue
            alias = ALIAS_RE.match(line)
            if alias:
                target = type_aliases if alias.group(1).startswith("!") else attr_aliases
                target[alias.group(1)] = alias.group(2).strip()
                continue

        if masked.startswith("}"):
            rest = line[1:].strip()
            if masked.rstrip().endswith("{") and top.kind in ("op", "func", "opaque", "module"):
                region = RawRegion(label=_region_label(rest))
                region.blocks[0].args = _trailer_region_args(rest)
                if top.kind == "op":
                    top.obj.trailers.append(rest)
                    top.obj.regions.append(region)
                top.region = region
                continue
            frame = stack.pop() if len(stack) > 1 else top
            if frame.kind == "op":
                frame.obj.trailers.append(rest)
                frame.obj.loc = last_loc(rest) or frame.obj.loc
            elif frame.kind == "func":
                frame.obj.loc = last_loc(rest)
            continue

        opens_region = masked.rstrip().endswith("{")

        if top.kind in ("root", "module"):
            if line.startswith("module"):
                name_match = re.match(r'^module\s+@("(?:[^"\\]|\\.)*"|[A-Za-z0-9_$.\-]+)', line)
                if name_match:
                    module_source = _strip_symbol(name_match.group(1))
                if opens_region:
                    stack.append(_Frame("module"))
                continue
            if line.startswith("cir.func"):
                fn = _parse_function_header(line, line_no)
                functions.append(fn)
                if opens_region:
                    fn.body = RawRegion()
                    fn.body.blocks[0].args = [(n, t) for n, t in fn.params if n]
                    stack.append(_Frame("func", fn, fn.body))
                else:
                    fn.loc = last_loc(line)
                continue
            if line.startswith("cir.global"):
                globals_.append(_parse_global(line, line_no))
            if opens_region:
                stack.append(_Frame("opaque", None, RawRegion()))
            continue

        if top.kind == "opaque":
            if opens_region:
                stack.append(_Frame("opaque", None, RawRegion()))
            continue

        block_match = BLOCK_RE.match(line)
        if block_match:
            is_flat = True
            args: List[Tuple[str, str]] = []
            if block_match.group(2):
                inner = block_match.group(2).strip()[1:-1]
                for arg in split_top_level(inner):
                    arg = strip_locs(arg)
                    arg_match = re.match(r"^(%[A-Za-z0-9_$.\-]+)\s*:\s*(.*)$", arg)
                    if arg_match:
                        args.append((arg_match.group(1), arg_match.group(2).strip()))
            region = top.region
            current = region.blocks[-1]
            if len(region.blocks) == 1 and current.label is None and not current.ops and (
                top.kind != "func" or not current.args
            ):
                current.label = block_match.group(1)
                current.args = args
            else:
                region.blocks.append(RawBlock(block_match.group(1), args))
            continue

        op = parse_operation(line, line_no)
        top.block.ops.append(op)
        if opens_region:
            region = RawRegion(label=_region_label(line))
            op.regions.append(region)
            stack.append(_Frame("op", op, region))

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return RawModule(
        path=path,
        sourcePath=module_source or path,
        contentHash=digest,
        locations=LocationTable(loc_raw),
        typeAliases=type_aliases,
        attrAliases=attr_aliases,
        functions=functions,
        globals=globals_,
        isFlat=is_flat,
    )


def parse_cir_file(path: str) -> RawModule:
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    return parse_cir_text(text, path)
