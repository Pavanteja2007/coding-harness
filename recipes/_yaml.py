"""Strict, dependency-free YAML subset used by portable recipes."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .models import Recipe, RecipeParseError, RecipeSecurityError, RecipeSubrecipeError

RecipeYAMLError = RecipeParseError

DEFAULT_MAX_BYTES = 64 * 1024
DEFAULT_MAX_DEPTH = 32
DEFAULT_MAX_NESTING = 64
DEFAULT_MAX_NODES = 4096
_NUMBER_RE = re.compile(r"^[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")
_INTEGER_RE = re.compile(r"^[+-]?(?:0|[1-9][0-9]*)$")
_BLOCK_HEADER_RE = re.compile(r"^[|>](?:[+-][1-9]?|[1-9][+-]?)?$")


@dataclass(frozen=True)
class _PhysicalLine:
    """One physical source line retained for block scalar fidelity."""

    number: int
    indent: int
    raw: str
    content: str


class _SubsetParser:
    """Parse the deliberately small recipe YAML grammar."""

    def __init__(
        self,
        text: str,
        *,
        max_depth: int,
        max_nesting: int,
        max_nodes: int,
        source: str,
    ) -> None:
        """Initialize parser limits and physical lines."""
        self.max_depth = max_depth
        self.max_nesting = max_nesting
        self.max_nodes = max_nodes
        self.source = source
        self.nodes = 0
        self.lines = self._prepare_lines(text)
        self.length = len(self.lines)

    def _prepare_lines(self, text: str) -> list[_PhysicalLine]:
        if not isinstance(text, str):
            raise RecipeParseError("recipe source must be UTF-8 text")
        if "\x00" in text:
            raise RecipeParseError("recipe source contains a NUL character")
        if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
            raise RecipeParseError("recipe source contains a control character")
        result: list[_PhysicalLine] = []
        nonblank_seen = False
        for number, raw in enumerate(text.splitlines(), start=1):
            if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
                raise RecipeParseError("tabs are not allowed for indentation")
            stripped = raw.strip()
            if stripped == "---" and not nonblank_seen:
                continue
            if stripped == "..." and nonblank_seen:
                break
            if stripped.startswith("%YAML") or stripped.startswith("%TAG"):
                raise RecipeParseError("YAML directives are not supported")
            indent = len(raw) - len(raw.lstrip(" "))
            content = _strip_comment(raw[indent:]).rstrip()
            result.append(_PhysicalLine(number, indent, raw, content))
            if stripped and not stripped.startswith("#"):
                nonblank_seen = True
        return result

    def parse(self) -> Any:
        """Parse one document and return a JSON-like Python value."""
        index = self._skip_ignorable(0)
        if index >= self.length:
            return None
        line = self.lines[index]
        if line.indent != 0:
            raise RecipeParseError("top-level recipe content must not be indented")
        value, next_index = self._parse_block(index, 0, 0)
        next_index = self._skip_ignorable(next_index)
        if next_index < self.length:
            raise RecipeParseError("multiple YAML documents are not supported")
        return value

    def _skip_ignorable(self, index: int) -> int:
        while index < self.length:
            content = self.lines[index].content.strip()
            if not content or content.startswith("#"):
                index += 1
                continue
            return index
        return index

    def _parse_block(self, index: int, indent: int, depth: int) -> tuple[Any, int]:
        if depth > self.max_depth:
            raise RecipeParseError("recipe YAML nesting exceeds the configured depth")
        index = self._skip_ignorable(index)
        if index >= self.length:
            raise RecipeParseError("unexpected end of recipe document")
        line = self.lines[index]
        if line.indent != indent:
            raise RecipeParseError("recipe YAML indentation is inconsistent")
        if line.content == "-" or line.content.startswith("- "):
            return self._parse_sequence(index, indent, depth)
        return self._parse_mapping(index, indent, depth)

    def _parse_mapping(
        self, index: int, indent: int, depth: int
    ) -> tuple[dict[str, Any], int]:
        if depth > self.max_depth:
            raise RecipeParseError("recipe YAML nesting exceeds the configured depth")
        result: dict[str, Any] = {}
        while True:
            index = self._skip_ignorable(index)
            if index >= self.length:
                break
            line = self.lines[index]
            if line.indent < indent:
                break
            if line.indent > indent:
                raise RecipeParseError("unexpected recipe YAML indentation")
            if line.content == "-" or line.content.startswith("- "):
                break
            key, remainder = self._split_key(line.content, line.number)
            if key == "<<":
                raise RecipeParseError("YAML merge keys are not supported")
            if key in result:
                raise RecipeParseError(f"duplicate recipe key: {key}")
            self._count_node()
            if remainder in {
                "|",
                ">",
                "|-",
                "|+",
                ">-",
                ">+",
            } or _BLOCK_HEADER_RE.fullmatch(remainder):
                value, index = self._parse_block_scalar(index, indent, remainder)
            elif remainder == "":
                next_index = self._skip_ignorable(index + 1)
                if next_index < self.length and self.lines[next_index].indent > indent:
                    child_indent = self.lines[next_index].indent
                    value, index = self._parse_block(
                        next_index, child_indent, depth + 1
                    )
                else:
                    value = None
                    index += 1
            else:
                value = self._parse_scalar(remainder, line.number)
                index += 1
            result[key] = value
        return result, index

    def _parse_sequence(
        self, index: int, indent: int, depth: int
    ) -> tuple[list[Any], int]:
        if depth > self.max_depth:
            raise RecipeParseError("recipe YAML nesting exceeds the configured depth")
        result: list[Any] = []
        while True:
            index = self._skip_ignorable(index)
            if index >= self.length:
                break
            line = self.lines[index]
            if line.indent < indent:
                break
            if line.indent > indent:
                raise RecipeParseError("unexpected recipe YAML indentation")
            if not (line.content == "-" or line.content.startswith("- ")):
                break
            self._count_node()
            remainder = line.content[1:].strip()
            if _BLOCK_HEADER_RE.fullmatch(remainder):
                parsed, index = self._parse_block_scalar(index, indent, remainder)
                result.append(parsed)
                continue
            if not remainder:
                next_index = self._skip_ignorable(index + 1)
                if next_index >= self.length or self.lines[next_index].indent <= indent:
                    result.append(None)
                    index += 1
                    continue
                child_indent = self.lines[next_index].indent
                value, index = self._parse_block(next_index, child_indent, depth + 1)
                result.append(value)
                continue
            mapping_entry = self._try_sequence_mapping_entry(remainder, line.number)
            if mapping_entry is not None:
                first_key, first_value = mapping_entry
                item: dict[str, Any] = {}
                if first_key == "<<":
                    raise RecipeParseError("YAML merge keys are not supported")
                if first_key in item:
                    raise RecipeParseError(f"duplicate recipe key: {first_key}")
                self._count_node()
                if first_value == "":
                    next_index = self._skip_ignorable(index + 1)
                    if (
                        next_index < self.length
                        and self.lines[next_index].indent > indent + 1
                    ):
                        child_indent = self.lines[next_index].indent
                        parsed, index = self._parse_block(
                            next_index, child_indent, depth + 1
                        )
                    else:
                        parsed = None
                        index += 1
                elif _BLOCK_HEADER_RE.fullmatch(first_value):
                    parsed, index = self._parse_block_scalar(
                        index, indent + 2, first_value
                    )
                else:
                    parsed = self._parse_scalar(first_value, line.number)
                    index += 1
                item[first_key] = parsed
                while True:
                    next_index = self._skip_ignorable(index)
                    if (
                        next_index >= self.length
                        or self.lines[next_index].indent <= indent
                    ):
                        index = next_index
                        break
                    continuation = self.lines[next_index]
                    if continuation.indent != indent + 2:
                        raise RecipeParseError(
                            "recipe sequence mapping indentation is inconsistent"
                        )
                    key, value = self._split_key(
                        continuation.content, continuation.number
                    )
                    if key == "<<":
                        raise RecipeParseError("YAML merge keys are not supported")
                    if key in item:
                        raise RecipeParseError(f"duplicate recipe key: {key}")
                    self._count_node()
                    if value == "":
                        nested_index = self._skip_ignorable(next_index + 1)
                        if (
                            nested_index < self.length
                            and self.lines[nested_index].indent > continuation.indent
                        ):
                            parsed_value, index = self._parse_block(
                                nested_index,
                                self.lines[nested_index].indent,
                                depth + 1,
                            )
                        else:
                            parsed_value = None
                            index = next_index + 1
                    elif _BLOCK_HEADER_RE.fullmatch(value):
                        parsed_value, index = self._parse_block_scalar(
                            next_index,
                            continuation.indent,
                            value,
                        )
                    else:
                        parsed_value = self._parse_scalar(value, continuation.number)
                        index = next_index + 1
                    item[key] = parsed_value
                result.append(item)
                continue
            if remainder.startswith(("[", "{")) or not self._looks_like_mapping(
                remainder
            ):
                result.append(self._parse_scalar(remainder, line.number))
                index += 1
            else:
                raise RecipeParseError("recipe sequence item is malformed")
        return result, index

    def _try_sequence_mapping_entry(
        self, remainder: str, number: int
    ) -> Optional[tuple[str, str]]:
        try:
            key, value = self._split_key(remainder, number)
        except RecipeParseError:
            return None
        if not key or key.startswith(("[", "{")) or " " in key:
            return None
        return key, value

    def _looks_like_mapping(self, remainder: str) -> bool:
        if remainder.startswith(("[", "{")):
            return False
        try:
            key, _ = self._split_key(remainder, 0)
        except RecipeParseError:
            return False
        return bool(key) and " " not in key

    def _split_key(self, content: str, number: int) -> tuple[str, str]:
        text = content.strip()
        if not text:
            raise RecipeParseError(f"line {number} has an empty mapping key")
        if text[0] in "\"'":
            key, consumed = _read_quoted(text, 0)
            remainder = text[consumed:].lstrip()
            if not remainder.startswith(":"):
                raise RecipeParseError(f"line {number} mapping key has no colon")
            key_text = str(key)
            remainder = remainder[1:].strip()
        else:
            colon = _find_plain_colon(text)
            if colon <= 0:
                raise RecipeParseError(f"line {number} is not a mapping entry")
            key_text = text[:colon].strip()
            remainder = text[colon + 1 :].strip()
        if key_text == "<<":
            return key_text, remainder
        if not key_text or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", key_text):
            raise RecipeParseError(f"line {number} mapping key is not safe")
        return key_text, remainder

    def _parse_scalar(self, value: str, number: int) -> Any:
        text = value.strip()
        if not text:
            return None
        if text[0] in "\"'":
            parsed, consumed = _read_quoted(text, 0)
            if text[consumed:].strip():
                raise RecipeParseError(f"line {number} has trailing scalar data")
            return parsed
        if text.startswith(("[", "{")):
            return _parse_json_collection(
                text, self.max_depth, self.max_nesting, number
            )
        _reject_yaml_specials(text, number)
        folded = text.casefold()
        if folded in {"true", "false"}:
            return folded == "true"
        if folded in {"null", "~"}:
            return None
        if _INTEGER_RE.fullmatch(text):
            return int(text)
        if _NUMBER_RE.fullmatch(text):
            value = float(text)
            if not math.isfinite(value):
                raise RecipeParseError(f"line {number} contains a non-finite number")
            return value
        return text

    def _parse_block_scalar(
        self, index: int, parent_indent: int, header: str
    ) -> tuple[str, int]:
        normalized_header = header.strip()
        if _BLOCK_HEADER_RE.fullmatch(normalized_header) is None:
            raise RecipeParseError("unsupported block scalar header")
        indicators = normalized_header[1:]
        chomp = next((item for item in indicators if item in "+-"), "")
        explicit = next((item for item in indicators if item.isdigit()), "")
        cursor = index + 1
        content_lines: list[str] = []
        content_indent = parent_indent + int(explicit) if explicit else None
        while cursor < self.length:
            line = self.lines[cursor]
            if line.raw.strip() and line.indent <= parent_indent:
                break
            if line.raw.strip():
                if content_indent is None:
                    content_indent = line.indent
                if line.indent < content_indent:
                    raise RecipeParseError("block scalar indentation is invalid")
                content_lines.append(line.raw[content_indent:])
            else:
                content_lines.append("")
            cursor += 1
        if content_indent is None:
            content_indent = parent_indent + int(explicit or 2)
        if normalized_header[0] == "|":
            text = _literal_block_text(content_lines)
        else:
            text = _folded_block_text(content_lines)
        if chomp == "-":
            text = text.rstrip("\n")
        elif chomp != "+":
            text = text.rstrip("\n") + "\n" if text else ""
        return text, cursor

    def _count_node(self) -> None:
        self.nodes += 1
        if self.nodes > self.max_nodes:
            raise RecipeParseError("recipe YAML contains too many values")


def _literal_block_text(lines: list[str]) -> str:
    """Render literal block lines with one physical line break per line."""
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _folded_block_text(lines: list[str]) -> str:
    """Render folded block lines while retaining blank and indented lines."""
    if not lines:
        return ""
    result: list[str] = []
    previous_more_indented = False
    previous_blank = False
    first = True
    for line in lines:
        if not line:
            result.append("\n")
            previous_blank = True
            previous_more_indented = False
            first = False
            continue
        more_indented = line[:1].isspace()
        if first or previous_blank:
            result.append(line)
        elif more_indented or previous_more_indented:
            result.append("\n")
            result.append(line)
        else:
            result.append(" ")
            result.append(line)
        first = False
        previous_blank = False
        previous_more_indented = more_indented
    result.append("\n")
    return "".join(result)


def _strip_comment(value: str) -> str:
    quote: Optional[str] = None
    escaped = False
    flow_depth = 0
    index = 0
    while index < len(value):
        char = value[index]
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if quote == "'":
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in "\"'":
            quote = char
        elif char in "[{":
            flow_depth += 1
        elif char in "]}":
            flow_depth = max(0, flow_depth - 1)
        elif (
            char == "#"
            and flow_depth == 0
            and (index == 0 or value[index - 1].isspace())
        ):
            return value[:index].rstrip()
        index += 1
    return value.rstrip()


def _read_quoted(text: str, start: int) -> tuple[str, int]:
    quote = text[start]
    if quote not in "\"'":
        raise RecipeParseError("expected a quoted scalar")
    if quote == "'":
        index = start + 1
        chars: list[str] = []
        while index < len(text):
            char = text[index]
            if char == "'":
                if index + 1 < len(text) and text[index + 1] == "'":
                    chars.append("'")
                    index += 2
                    continue
                return "".join(chars), index + 1
            chars.append(char)
            index += 1
        raise RecipeParseError("unterminated single-quoted scalar")
    try:
        value, consumed = json.JSONDecoder().raw_decode(text[start:])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecipeParseError("invalid double-quoted scalar") from exc
    if not isinstance(value, str):
        raise RecipeParseError("double-quoted scalar did not decode to text")
    return value, start + consumed


def _find_plain_colon(value: str) -> int:
    quote: Optional[str] = None
    for index, char in enumerate(value):
        if char in "\"'":
            quote = None if quote == char else char if quote is None else quote
        if (
            quote is None
            and char == ":"
            and (index + 1 == len(value) or value[index + 1].isspace())
        ):
            return index
    return -1


def _reject_yaml_specials(value: str, number: int) -> None:
    quote: Optional[str] = None
    escaped = False
    for char in value:
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if quote == "'":
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "&*!":
            raise RecipeParseError(
                f"line {number} uses an unsupported YAML tag, anchor, or alias"
            )


def _parse_json_collection(
    text: str,
    max_depth: int,
    max_nesting: int,
    number: int,
) -> Any:
    _check_flow_depth(text, max_nesting, number)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                RecipeParseError(f"line {number} contains a non-finite JSON number")
            ),
        )
    except RecipeParseError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise RecipeParseError(f"line {number} contains invalid inline JSON") from exc
    _check_value_depth(value, max_nesting, number)
    return value


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key == "<<":
            raise RecipeParseError("YAML merge keys are not supported")
        if key in result:
            raise RecipeParseError(f"duplicate recipe key: {key}")
        result[key] = value
    return result


def _check_flow_depth(text: str, maximum: int, number: int) -> None:
    quote: Optional[str] = None
    escaped = False
    depth = 0
    for char in text:
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char == '"':
            quote = char
        elif char in "[{":
            depth += 1
            if depth > maximum:
                raise RecipeParseError(
                    f"line {number} inline collection is too deeply nested"
                )
        elif char in "]}":
            depth -= 1


def _check_value_depth(value: Any, maximum: int, number: int) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise RecipeParseError(f"line {number} contains a non-finite number")
    if isinstance(value, dict):
        if maximum < 0:
            raise RecipeParseError(
                f"line {number} inline collection is too deeply nested"
            )
        for key, item in value.items():
            _check_value_depth(key, maximum - 1, number)
            _check_value_depth(item, maximum - 1, number)
    elif isinstance(value, list):
        if maximum < 0:
            raise RecipeParseError(
                f"line {number} inline collection is too deeply nested"
            )
        for item in value:
            _check_value_depth(item, maximum - 1, number)


def parse_yaml_subset(
    text: str | bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nesting: Optional[int] = None,
    max_nodes: int = DEFAULT_MAX_NODES,
    source: str = "<string>",
) -> Any:
    """Parse supported recipe YAML without importing a YAML object loader."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise RecipeParseError("recipe source is not valid UTF-8") from exc
    if not isinstance(text, str):
        raise RecipeParseError("recipe source must be UTF-8 text")
    text = text.lstrip("\ufeff")
    if max_bytes <= 0 or max_depth <= 0 or max_nodes <= 0:
        raise RecipeParseError("recipe parser limits must be positive")
    nesting = max_depth if max_nesting is None else max_nesting
    if nesting <= 0:
        raise RecipeParseError("recipe parser nesting limit must be positive")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RecipeParseError("recipe source is not valid UTF-8") from exc
    if len(encoded) > max_bytes:
        raise RecipeParseError("recipe file exceeds the configured size limit")
    return _SubsetParser(
        text,
        max_depth=max_depth,
        max_nesting=nesting,
        max_nodes=max_nodes,
        source=source,
    ).parse()


parse_recipe_yaml = parse_yaml_subset
safe_load_recipe_yaml = parse_yaml_subset
safe_load = parse_yaml_subset
loads = parse_yaml_subset


def parse_recipe(
    text: str,
    *,
    source: str = "<string>",
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nesting: Optional[int] = None,
    max_nodes: int = DEFAULT_MAX_NODES,
    validate: bool = True,
    tool_catalog: Any = None,
) -> Recipe:
    """Parse and optionally schema-validate one recipe document."""
    data = parse_yaml_subset(
        text,
        max_bytes=max_bytes,
        max_depth=max_depth,
        max_nesting=max_nesting,
        max_nodes=max_nodes,
        source=source,
    )
    if data is None:
        raise RecipeParseError("recipe document is empty")
    recipe = Recipe.from_dict(data, source=source)
    if validate:
        from .validator import RecipeValidator

        RecipeValidator(tool_catalog=tool_catalog, max_depth=max_depth).validate(recipe)
    return recipe


def _read_source(source: Any, *, max_bytes: int) -> tuple[str, str]:
    if isinstance(source, bytes):
        if len(source) > max_bytes:
            raise RecipeParseError("recipe file exceeds the configured size limit")
        try:
            return source.decode("utf-8-sig"), "<bytes>"
        except UnicodeDecodeError as exc:
            raise RecipeParseError("recipe source is not valid UTF-8") from exc
    if hasattr(source, "read"):
        try:
            raw = source.read()
        except Exception as exc:
            raise RecipeParseError("recipe source could not be read") from exc
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RecipeParseError("recipe source is not valid UTF-8") from exc
        else:
            text = str(raw)
        return text, str(getattr(source, "name", "<stream>"))
    if isinstance(source, Path):
        path = source
    elif isinstance(source, str):
        candidate = Path(source)
        if "\n" not in source and "\r" not in source and len(source) < 4096:
            try:
                if candidate.is_file():
                    path = candidate
                else:
                    return source, "<string>"
            except OSError:
                return source, "<string>"
        else:
            return source, "<string>"
    else:
        raise RecipeParseError("recipe source must be text, a path, or a stream")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RecipeParseError("recipe file could not be read") from exc
    if len(raw) > max_bytes:
        raise RecipeParseError("recipe file exceeds the configured size limit")
    try:
        return raw.decode("utf-8-sig"), str(path)
    except UnicodeDecodeError as exc:
        raise RecipeParseError("recipe file is not valid UTF-8") from exc


def load_recipe(
    source: Any,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nesting: Optional[int] = None,
    max_nodes: int = DEFAULT_MAX_NODES,
    validate: bool = True,
    tool_catalog: Any = None,
) -> Recipe:
    """Load a recipe from a path, stream, or raw YAML string."""
    text, source_name = _read_source(source, max_bytes=max_bytes)
    return parse_recipe(
        text,
        source=source_name,
        max_bytes=max_bytes,
        max_depth=max_depth,
        max_nesting=max_nesting,
        max_nodes=max_nodes,
        validate=validate,
        tool_catalog=tool_catalog,
    )


load_recipe_file = load_recipe
load_recipe_yaml = load_recipe
load_yaml = parse_yaml_subset
load_recipe_document = parse_recipe
loads_recipe = parse_recipe


def load_recipe_directory(
    root: Any,
    *,
    pattern: str = "*.y*ml",
    **kwargs: Any,
) -> dict[str, Recipe]:
    """Load all recipe files in a directory using deterministic filename order."""
    directory = Path(root)
    if not directory.is_dir():
        raise RecipeParseError("recipe directory does not exist")
    result: dict[str, Recipe] = {}
    for path in sorted(directory.glob(pattern), key=lambda item: item.name):
        if not path.is_file():
            continue
        result[path.stem] = load_recipe(path, **kwargs)
    return result


def load_builtin_recipes(
    *,
    builtin_root: Any = None,
    **kwargs: Any,
) -> dict[str, Recipe]:
    """Load all bundled recipe documents in deterministic filename order."""
    root = (
        Path(builtin_root)
        if builtin_root is not None
        else Path(__file__).parent / "builtin"
    )
    return load_recipe_directory(root, **kwargs)


def load_builtin_recipe(
    name: str,
    *,
    builtin_root: Any = None,
    **kwargs: Any,
) -> Recipe:
    """Load a named recipe from the bundled portable built-in directory."""
    if (
        not isinstance(name, str)
        or not name
        or any(part in {"", ".", ".."} for part in name.replace("\\", "/").split("/"))
        or any(token in name.casefold() for token in ("%2e", "%2f", "%5c"))
    ):
        raise RecipeSecurityError("built-in recipe reference is unsafe")
    root = (
        Path(builtin_root)
        if builtin_root is not None
        else Path(__file__).parent / "builtin"
    )
    candidates = [root / f"{name}.yaml", root / f"{name}.yml", root / name]
    for candidate in candidates:
        if candidate.is_file():
            return load_recipe(candidate, **kwargs)
    raise RecipeSubrecipeError(f"built-in recipe not found: {name}")


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_NODES",
    "RecipeYAMLError",
    "load_builtin_recipe",
    "load_builtin_recipes",
    "load_recipe",
    "load_recipe_directory",
    "load_recipe_document",
    "load_recipe_file",
    "load_recipe_yaml",
    "load_yaml",
    "loads",
    "loads_recipe",
    "parse_recipe",
    "parse_recipe_yaml",
    "parse_yaml_subset",
    "safe_load",
    "safe_load_recipe_yaml",
]
