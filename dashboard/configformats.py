"""Lossless configuration edits. Lua is parsed as literal data, never executed."""

import json
import difflib
import hashlib
import math
import re
from bisect import bisect_left

SECRET = "__PZ_SECRET_UNCHANGED__"
SECRET_KEY = re.compile(r"password|token|secret", re.I)
MOD_KEYS = {"Mods", "WorkshopItems", "Map"}
INI_KEYS = {
    "mods": "Mods",
    "workshopitems": "WorkshopItems",
    "map": "Map",
    "password": "Password",
    "rconpassword": "RCONPassword",
}


class FormatError(ValueError):
    pass


def preserve_newlines(original, edited):
    """Browser textareas normalize CRLF. Retain each unchanged line's convention."""
    if not original:
        return edited

    def ending(line):
        return (
            "\r\n"
            if line.endswith("\r\n")
            else "\n"
            if line.endswith("\n")
            else "\r"
            if line.endswith("\r")
            else ""
        )

    before, after = original.splitlines(True), edited.splitlines(True)
    before_body = [line[: -len(ending(line))] if ending(line) else line for line in before]
    after_body = [line[: -len(ending(line))] if ending(line) else line for line in after]
    default = (
        "\r\n" if original.count("\r\n") > original.count("\n") - original.count("\r\n") else "\n"
    )
    result = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, before_body, after_body, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            for original_index, edited_index in zip(range(i1, i2), range(j1, j2), strict=True):
                line = before[original_index]
                if not ending(line) and edited_index < len(after) - 1:
                    line += default
                result.append(line)
        elif tag != "delete":
            for offset, index in enumerate(range(j1, j2)):
                tail = (ending(before[i1 + offset]) if i1 + offset < i2 else default) or default
                result.append(after_body[index] + (tail if ending(after[index]) else ""))
    combined = "".join(result)
    if after and not ending(after[-1]):
        combined = combined.rstrip("\r\n")
    elif after and not ending(combined):
        combined += default
    return combined


def ini_entries(text):
    entries = {}
    pattern = re.compile(r"^([ \t]*)([^#;\s=][^=\r\n]*?)([ \t]*=[ \t]*)([^\r\n]*)", re.M)
    for match in pattern.finditer(text):
        key = match[2].strip()
        key = INI_KEYS.get(key.lower(), key)
        end = match.end(4)
        value = match[4]
        while value.rstrip().endswith(";"):
            continuation = re.match(r"\r?\n[ \t]+([^\r\n]*)", text[end:])
            if (
                not continuation
                or not continuation[1].strip()
                or continuation[1].lstrip().startswith(("#", ";"))
                or "=" in continuation[1]
            ):
                break
            value += continuation[1].strip()
            end += continuation.end()
        if key in entries:
            raise FormatError(f"Повторяющийся ключ INI: {key}")
        entries[key] = {"value": value, "start": match.start(4), "end": end}
    return entries


def edit_ini(text, changes):
    entries = ini_entries(text)
    edits = []
    seen = set()
    newline = "\r\n" if "\r\n" in text else "\n"
    for key, value in changes.items():
        if not isinstance(key, str):
            raise FormatError("Некорректный ключ INI")
        key = INI_KEYS.get(key.lower(), key)
        if key not in entries and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
            raise FormatError("Некорректный ключ INI")
        if key in seen:
            raise FormatError(f"Повторяющийся ключ INI: {key}")
        seen.add(key)
        value = str(value)
        if "\n" in value or "\r" in value or "\0" in value:
            raise FormatError(f"Перенос строки недопустим в {key}")
        if key in entries:
            entry = entries[key]
            edits.append((entry["start"], entry["end"], value))
        else:
            text += ("" if text.endswith(("\n", "\r")) else newline) + f"{key}={value}{newline}"
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    return text


def mask_ini(text):
    return edit_ini(text, {key: SECRET for key in ini_entries(text) if SECRET_KEY.search(key)})


def restore_ini_secrets(text, original):
    old = ini_entries(original)
    return edit_ini(
        text,
        {
            key: old[key]["value"]
            for key, rec in ini_entries(text).items()
            if SECRET_KEY.search(key) and rec["value"] == SECRET and key in old
        },
    )


def normalize_mod(value):
    return value.strip().lstrip("\\")


def split_list(value, mods=False):
    return [normalize_mod(v) if mods else v.strip() for v in value.split(";") if v.strip()]


def literal(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        # Lua supports these escapes; JSON's unicode escapes do not belong in Lua.
        return re.sub(
            r"\\u00([0-9a-f]{2})",
            lambda m: "\\" + str(int(m[1], 16)).zfill(3),
            json.dumps(value, ensure_ascii=False),
        )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value):
                return str(value)
        except OverflowError:
            pass
    raise FormatError("Поддерживаются строки, конечные числа и boolean")


STRING_TOKEN = r"|(?P<string>\"(?:\\(?:\r\n|.)|[^\"\\\r\n])*\"|'(?:\\(?:\r\n|.)|[^'\\\r\n])*')"
TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<comment>--\[(?P<eq>=*)\[.*?\](?P=eq)\]|--[^\r\n]*)"
    r"|(?P<long>\[(?P<leq>=*)\[.*?\](?P=leq)\])"
    + STRING_TOKEN
    + r"|(?P<number>-?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]*)|(?P<punct>[{}()\[\]=,;])",
    re.S,
)
RAW_TOKEN = re.compile(
    TOKEN.pattern.replace(
        STRING_TOKEN, r"|(?P<string>\"(?:\\.|[^\"\\])*(?:\"|$)|'(?:\\.|[^'\\])*(?:'|$))"
    ).replace(
        r"(?P<long>\[(?P<leq>=*)\[.*?\](?P=leq)\])",
        r"(?P<long>\[(?P<leq>=*)\[.*?(?:\](?P=leq)\]|$))",
    ),
    re.S,
)


def decode_string(raw):
    if raw.startswith("["):
        match = re.match(r"\[(=*)\[", raw)
        if not match:
            raise FormatError("Некорректная длинная строка Lua")
        body = raw[match.end() : -len(match[0])]
        return re.sub(r"^(?:\r\n|\n|\r)", "", body, count=1)
    if not raw.startswith(('"', "'")) or raw[-1] != raw[0]:
        raise FormatError("Ключ таблицы должен быть строковым литералом")
    body = raw[1:-1]
    escapes = {
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "\\": "\\",
        '"': '"',
        "'": "'",
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "v": "\v",
    }

    def unescape(match):
        decimal = match["decimal"]
        if decimal is not None:
            numbers = [int(number) for number in decimal.split("\\")]
            if any(number > 255 for number in numbers):
                raise FormatError("Некорректная escape-последовательность Lua")
            data = bytes(numbers)
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                # Keep the legacy byte-to-codepoint view for non-UTF-8 runs.
                # Unlike surrogate escapes, this stays safe for JSON responses.
                return data.decode("latin-1")
        token = match["escape"]
        if token in ("\n", "\r\n", "\r"):
            return "\n"
        if token not in escapes:
            raise FormatError("Неподдерживаемая escape-последовательность Lua")
        return escapes[token]

    return re.sub(
        r"\\(?P<decimal>\d{1,3}(?:\\\d{1,3})*)|\\(?P<escape>\r\n|\n|\r|.)", unescape, body
    )


def version_marker(text):
    """Find the root service field without matching comments, strings or mod tables."""
    tokens = [
        match for match in TOKEN.finditer(text) if match.lastgroup not in ("comment", "space")
    ]
    start = next(
        (
            i + 2
            for i, token in enumerate(tokens[:-2])
            if token[0] == "SandboxVars" and tokens[i + 1][0] == "=" and tokens[i + 2][0] == "{"
        ),
        None,
    )
    if start is None:
        return None
    depth = 0
    for i in range(start, len(tokens)):
        token = tokens[i][0]
        is_name = token == "VERSION"
        if (
            depth == 1
            and tokens[i].lastgroup == "string"
            and i > 0
            and tokens[i - 1][0] == "["
            and i + 1 < len(tokens)
            and tokens[i + 1][0] == "]"
        ):
            is_name = decode_string(token) == "VERSION"
        equal = i + 1 if token == "VERSION" else i + 2
        if depth == 1 and is_name and equal < len(tokens) and tokens[equal][0] == "=":
            at = tokens[equal].end()
            level = 0
            end = len(text)
            for value_token in tokens[equal + 1 :]:
                value = value_token[0]
                if level == 0 and value in (",", ";", "}"):
                    end = value_token.start()
                    break
                if value in ("{", "("):
                    level += 1
                elif value in ("}", ")"):
                    level -= 1
            raw = text[at:end]
            clean = []
            offset = 0
            for match in TOKEN.finditer(raw):
                clean.append(raw[offset : match.start()])
                if match.lastgroup not in ("comment", "space"):
                    clean.append(match[0])
                offset = match.end()
            clean.append(raw[offset:])
            return "".join(clean).strip()
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
            if depth == 0:
                break
    return None


class LuaTable:
    def __init__(self, text):
        self.text = text
        self.tokens = []
        self.values = {}
        self.tables = {}
        offset = 0
        while offset < len(text):
            token = TOKEN.match(text, offset)
            if not token:
                raise FormatError(f"Неподдерживаемое выражение Lua, позиция {offset + 1}")
            if token.lastgroup not in ("space", "comment"):
                self.tokens.append((token[0], token.start(), token.end()))
            offset = token.end()
        self.pos = 0
        self.take("SandboxVars")
        self.take("=")
        self.table(())
        if self.peek() == ";":
            self.take()
        if self.pos != len(self.tokens):
            raise FormatError("После SandboxVars обнаружен исполняемый код")

    def peek(self):
        return self.tokens[self.pos][0] if self.pos < len(self.tokens) else ""

    def take(self, expected=None):
        if self.pos >= len(self.tokens) or expected and self.peek() != expected:
            raise FormatError(f"Ожидалось {expected or 'значение'}")
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def table(self, path):
        if len(path) > 32:
            raise FormatError("Слишком глубокая вложенность SandboxVars")
        self.take("{")
        keys = set()
        while self.peek() != "}":
            start = self.tokens[self.pos][1] if self.pos < len(self.tokens) else 0
            if self.peek() == "[":
                self.take()
                key = decode_string(self.take()[0])
                self.take("]")
            else:
                key = self.take()[0]
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in {
                    "and",
                    "break",
                    "do",
                    "else",
                    "elseif",
                    "end",
                    "false",
                    "for",
                    "function",
                    "if",
                    "in",
                    "local",
                    "nil",
                    "not",
                    "or",
                    "repeat",
                    "return",
                    "then",
                    "true",
                    "until",
                    "while",
                }:
                    raise FormatError("Таблица SandboxVars должна иметь именованные ключи")
            if "." in key:
                raise FormatError("Ключ с точкой доступен только в редакторе исходника")
            if key in keys:
                raise FormatError(f"Повторяющийся ключ Lua: {key}")
            keys.add(key)
            self.take("=")
            child = (*path, key)
            if self.peek() == "{":
                self.table(child)
            else:
                raw, begin, end = self.take()
                if raw in ("true", "false"):
                    value = raw == "true"
                elif raw.startswith(('"', "'", "[")):
                    value = decode_string(raw)
                else:
                    try:
                        value = (
                            int(raw, 16)
                            if re.fullmatch(r"-?0[xX][0-9a-fA-F]+", raw)
                            else float(raw)
                            if any(c in raw.lower() for c in ".e")
                            else int(raw)
                        )
                    except ValueError:
                        raise FormatError("Lua-функции и вычисления не поддерживаются") from None
                    try:
                        finite = math.isfinite(value)
                    except OverflowError:
                        finite = False
                    if not finite:
                        raise FormatError("Неконечное число Lua")
                entry_end = end
                if self.peek() in (",", ";"):
                    entry_end = self.take()[2]
                elif self.peek() != "}":
                    raise FormatError("Между полями Lua нужна запятая")
                self.values[child] = {
                    "value": value,
                    "start": begin,
                    "end": end,
                    "entryStart": start,
                    "entryEnd": entry_end,
                }
                continue
            if self.peek() in (",", ";"):
                self.take()
            elif self.peek() != "}":
                raise FormatError("Между таблицами нужна запятая")
        self.tables[path] = self.take()[1]

    def edit(self, changes, remove=()):
        edits = []
        additions = {}
        for dotted, value in changes.items():
            path = tuple(dotted.split("."))
            if path == ("VERSION",):
                raise FormatError("VERSION сохраняется автоматически")
            if path in self.values:
                rec = self.values[path]
                edits.append((rec["start"], rec["end"], literal(value)))
            else:
                parent = path[:-1]
                while parent not in self.tables:
                    parent = parent[:-1]
                branch = additions.setdefault(parent, {})
                for key in path[len(parent) : -1]:
                    branch = branch.setdefault(key, {})
                    if not isinstance(branch, dict):
                        raise FormatError("Поле одновременно задано как таблица и значение")
                if path[-1] in branch:
                    raise FormatError("Поле одновременно задано как таблица и значение")
                branch[path[-1]] = value
        for dotted in remove:
            path = tuple(dotted.split("."))
            if path == ("VERSION",) or dotted in changes:
                raise FormatError("Нельзя удалить VERSION или одновременно изменяемое поле")
            if path in self.values:
                rec = self.values[path]
                edits.append((rec["entryStart"], rec["entryEnd"], ""))
        newline = "\r\n" if "\r\n" in self.text else "\n"

        def tree_literal(value):
            if isinstance(value, dict):
                return (
                    "{ "
                    + " ".join(f"[{literal(k)}] = {tree_literal(v)}," for k, v in value.items())
                    + " }"
                )
            return literal(value)

        for parent, values in additions.items():
            content = "".join(
                f"{newline}    [{literal(key)}] = {tree_literal(value)},"
                for key, value in values.items()
            )
            at = self.tables[parent]
            # A previously unterminated last entry needs a separator before insertion.
            last = next((t[0] for t in reversed(self.tokens) if t[2] <= at), "{")
            content = ("," if last not in ("{", ",", ";") else "") + content + newline
            edits.append((at, at, content))
        text = self.text
        for start, end, value in sorted(edits, reverse=True):
            text = text[:start] + value + text[end:]
        LuaTable(text)
        return text


def mask_lua(text, *, comparison=None, side=None):
    """Mask source, with optional value-independent change labels for reviews."""
    try:
        table = LuaTable(text)
        return table.edit(
            {".".join(p): SECRET for p in table.values if SECRET_KEY.search(".".join(p))}
        )
    except FormatError:
        # Unknown expressions may contain secrets. Hide every literal in raw mode.
        records = raw_literals(text)
        other = raw_literals(comparison) if comparison is not None else None
        for index in reversed(range(len(records))):
            start, end, placeholder, value = records[index]
            if other is not None and (index >= len(other) or value != other[index][3]):
                placeholder = placeholder[:-2] + f"_changed_{side}__"
            text = text[:start] + literal(placeholder) + text[end:]
        return text


def mask_lua_pair(before, after):
    """Keep raw literal edits visible without exporting values or value hashes."""
    return (
        mask_lua(before, comparison=after, side="before"),
        mask_lua(after, comparison=before, side="after"),
    )


def restore_lua_secrets(text, original):
    """Restore untouched secret literals byte-for-byte, including across expressions.

    Only direct fields in named tables can retain a secret marker. Expressions are
    skipped by balanced delimiters; no submitted Lua is executed or evaluated.
    """
    old = literal_table(original)
    tokens = [
        match for match in TOKEN.finditer(text) if match.lastgroup not in ("space", "comment")
    ]
    edits = []

    def trivia(begin, end):
        while begin < end:
            token = TOKEN.match(text, begin)
            if not token or token.lastgroup not in ("space", "comment") or token.end() > end:
                return False
            begin = token.end()
        return True

    def expression_end(index):
        delimiters, blocks = [], []
        while index < len(tokens):
            value = tokens[index][0]
            if not delimiters and not blocks and value in (",", ";", "}"):
                break
            if value in ("{", "(", "["):
                delimiters.append(value)
            elif value in ("}", ")", "]") and delimiters:
                delimiters.pop()
            elif value in ("function", "if", "do", "repeat"):
                blocks.append(value)
            elif value in ("end", "until") and blocks:
                blocks.pop()
            index += 1
        return index

    def table(index, path):
        if len(path) > 32:
            raise FormatError("Слишком глубокая вложенность SandboxVars")
        index += 1  # Opening brace.
        keys = set()
        found = []
        while index < len(tokens) and tokens[index][0] != "}":
            key, equal = None, index + 1
            if tokens[index].lastgroup == "name":
                key = tokens[index][0]
            elif (
                tokens[index][0] == "["
                and index + 3 < len(tokens)
                and tokens[index + 1].lastgroup in ("string", "long")
                and tokens[index + 2][0] == "]"
            ):
                key, equal = decode_string(tokens[index + 1][0]), index + 3
            if key is not None and equal < len(tokens) and tokens[equal][0] == "=":
                if key in keys:
                    raise FormatError(f"Повторяющийся ключ Lua: {key}")
                keys.add(key)
                child = (*path, key)
                start = equal + 1
                end = expression_end(start)
                if start < len(tokens) and tokens[start][0] == "{":
                    after, nested = table(start, child)
                    if after == end:
                        found.extend(nested)
                elif end == start + 1 and (
                    tokens[start].lastgroup in ("number", "string", "long")
                    or tokens[start][0] in ("true", "false")
                ):
                    begin = tokens[equal].end()
                    finish = tokens[end].start() if end < len(tokens) else len(text)
                    if trivia(begin, tokens[start].start()) and trivia(tokens[start].end(), finish):
                        prefix = "SandboxVars={Value="
                        marker = LuaTable(prefix + text[begin:finish] + "}")
                        if (
                            old
                            and child in old.values
                            and SECRET_KEY.search(".".join(child))
                            and marker.values.get(("Value",), {}).get("value") == SECRET
                        ):
                            rec, previous = marker.values[("Value",)], old.values[child]
                            found.append(
                                (
                                    begin + rec["start"] - len(prefix),
                                    begin + rec["end"] - len(prefix),
                                    original[previous["start"] : previous["end"]],
                                )
                            )
            else:
                end = expression_end(index)
            if end >= len(tokens):
                return end, found
            index = end + (tokens[end][0] in (",", ";"))
        return index + 1, found

    if [token[0] for token in tokens[:3]] == ["SandboxVars", "=", "{"]:
        _, edits = table(2, ())
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    if SECRET in text:
        raise FormatError("Неизвестный маркер литерала Lua")
    return text


def raw_literals(text):
    result = []
    for match in RAW_TOKEN.finditer(text):
        if match.lastgroup in ("string", "long", "number"):
            placeholder = (
                "__PZ_RAW_LITERAL_"
                + str(len(result))
                + "_"
                + hashlib.sha256(f"position:{len(result)}".encode()).hexdigest()[:12]
                + "__"
            )
            result.append((match.start(), match.end(), placeholder, match[0]))
    return result


def raw_literal_contexts(text, records, retained=None):
    """Locate literal slots without interpreting submitted expressions.

    Named fields have independent contexts. Unnamed/computed structures retain
    an exact token context, so ambiguous edits cannot move hidden values.
    """
    retained = retained or {}
    tokens = [
        token for token in RAW_TOKEN.finditer(text) if token.lastgroup not in ("space", "comment")
    ]
    literal_indexes = {rec[0]: index for index, rec in enumerate(records)}
    literal_starts = [rec[0] for rec in records]

    def signature(begin, end, generalize=False):
        parts, offset = [], begin
        for token in RAW_TOKEN.finditer(text, begin, end):
            gap = re.sub(r"\s+", "", text[offset : token.start()])
            if gap:
                parts.append(("operator", gap))
            if token.lastgroup in ("string", "long", "number"):
                value = None if generalize else retained.get(token.start(), token[0])
                parts.append(("literal", value))
            elif token.lastgroup not in ("space", "comment"):
                parts.append(("token", token[0]))
            offset = token.end()
        gap = re.sub(r"\s+", "", text[offset:end])
        if gap:
            parts.append(("operator", gap))
        return tuple(parts)

    fallback = signature(0, len(text))
    contexts = {index: ("ambiguous", fallback, index) for index in range(len(records))}

    def expression_end(index):
        delimiters, blocks = [], []
        while index < len(tokens):
            value = tokens[index][0]
            if not delimiters and not blocks and value in (",", ";", "}"):
                break
            if value in ("{", "(", "["):
                delimiters.append(value)
            elif value in ("}", ")", "]") and delimiters:
                delimiters.pop()
            elif value in ("function", "if", "do", "repeat"):
                blocks.append(value)
            elif value in ("end", "until") and blocks:
                blocks.pop()
            index += 1
        return index

    def table(index, path):
        if len(path) > 32:
            return None
        index += 1
        keys, found = set(), {}
        while index < len(tokens) and tokens[index][0] != "}":
            key, key_literal, equal = None, None, index + 1
            if tokens[index].lastgroup == "name":
                key = tokens[index][0]
            elif (
                tokens[index][0] == "["
                and index + 3 < len(tokens)
                and tokens[index + 1].lastgroup in ("string", "long")
                and tokens[index + 2][0] == "]"
            ):
                key_token = tokens[index + 1]
                try:
                    key = decode_string(retained.get(key_token.start(), key_token[0]))
                except FormatError:
                    return None
                key_literal, equal = literal_indexes[key_token.start()], index + 3
            if key is None or key in keys or equal >= len(tokens) or tokens[equal][0] != "=":
                return None
            keys.add(key)
            child, start = (*path, key), equal + 1
            end = expression_end(start)
            if end >= len(tokens):
                return None
            begin, finish = tokens[equal].end(), tokens[end].start()
            simple = not any(
                token[0] in ("{", "[", "function", "if", "do", "repeat")
                for token in tokens[start:end]
            )
            shape = signature(begin, finish, generalize=simple)
            slots = range(bisect_left(literal_starts, begin), bisect_left(literal_starts, finish))
            found.update({i: ("field", child, shape, slot) for slot, i in enumerate(slots)})
            if start < len(tokens) and tokens[start][0] == "{":
                nested = table(start, child)
                if nested and nested[0] == end:
                    found.update(nested[1])
            if key_literal is not None:
                found[key_literal] = ("key", child)
            index = end + (tokens[end][0] in (",", ";"))
        return (index + 1, found) if index < len(tokens) else None

    if [token[0] for token in tokens[:3]] == ["SandboxVars", "=", "{"]:
        named = table(2, ())
        if named:
            contexts.update(named[1])
    return contexts


def restore_raw_literals(text, original):
    previous, current = raw_literals(original), raw_literals(text)
    retained, used = {}, set()
    edits = []
    for index, (start, end, _, value) in enumerate(current):
        if not value.startswith(('"', "'", "[")):
            continue
        try:
            marker = decode_string(value)
        except FormatError:
            if "__PZ_RAW_LITERAL_" in value:
                raise FormatError("Неизвестный маркер литерала Lua") from None
            continue
        if "__PZ_RAW_LITERAL_" not in marker:
            continue
        match = re.fullmatch(r"__PZ_RAW_LITERAL_([0-9]{1,6})_[a-f0-9]{12}__", marker)
        slot = int(match[1]) if match else -1
        if not 0 <= slot < len(previous) or slot in used:
            raise FormatError("Неизвестный маркер литерала Lua")
        # Legacy suffixes once fingerprinted values. Treat every syntactically
        # valid suffix alike; ownership comes from context, never a secret hash.
        used.add(slot)
        retained[start] = previous[slot][3]
        edits.append((start, end, previous[slot][3], slot, index))
    unresolved = text
    for start, end, *_ in reversed(edits):
        unresolved = unresolved[:start] + unresolved[end:]
    if "__PZ_RAW_LITERAL_" in unresolved:
        raise FormatError("Неизвестный маркер литерала Lua")
    if edits:
        before = raw_literal_contexts(original, previous)
        after = raw_literal_contexts(text, current, retained)
        if any(before[slot] != after[index] for _, _, _, slot, index in edits):
            raise FormatError("Неизвестный маркер литерала Lua")
    for start, end, value, *_ in reversed(edits):
        text = text[:start] + value + text[end:]
    if "__PZ_RAW_LITERAL_" in text:
        raise FormatError("Неизвестный маркер литерала Lua")
    return text


def literal_table(text):
    try:
        return LuaTable(text)
    except FormatError:
        return None
