"""Lossless configuration edits. Lua is parsed as literal data, never executed."""

import json
import difflib
import hashlib
import math
import re

SECRET = "__PZ_SECRET_UNCHANGED__"
SECRET_KEY = re.compile(r"password|token|secret", re.I)
MOD_KEYS = {"Mods", "WorkshopItems", "Map"}


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
            result.extend(before[i1:i2])
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
        key = {
            "mods": "Mods",
            "workshopitems": "WorkshopItems",
            "map": "Map",
            "password": "Password",
            "rconpassword": "RCONPassword",
        }.get(key.lower(), key)
        end = match.end(4)
        value = match[4]
        while value.rstrip().endswith(";"):
            continuation = re.match(r"\r?\n[ \t]+([^\r\n]*)", text[end:])
            if not continuation or "=" in continuation[1]:
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
    newline = "\r\n" if "\r\n" in text else "\n"
    for key, value in changes.items():
        if (
            not isinstance(key, str)
            or key not in entries
            and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key)
        ):
            raise FormatError("Некорректный ключ INI")
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
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return str(value)
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
        token = match[1]
        if token in ("\n", "\r\n", "\r"):
            return "\n"
        if token.isdigit():
            number = int(token)
            if number > 255:
                raise FormatError("Некорректная escape-последовательность Lua")
            return chr(number)
        if token not in escapes:
            raise FormatError("Неподдерживаемая escape-последовательность Lua")
        return escapes[token]

    return re.sub(r"\\(\d{1,3}|\r\n|\n|\r|.)", unescape, body)


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
                    if not math.isfinite(value):
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


def mask_lua(text):
    try:
        table = LuaTable(text)
        return table.edit(
            {".".join(p): SECRET for p in table.values if SECRET_KEY.search(".".join(p))}
        )
    except FormatError:
        # Unknown expressions may contain secrets. Hide every literal in raw mode.
        for start, end, placeholder, _ in reversed(raw_literals(text)):
            text = text[:start] + literal(placeholder) + text[end:]
        return text


def raw_literals(text):
    result = []
    for match in RAW_TOKEN.finditer(text):
        if match.lastgroup in ("string", "long", "number"):
            placeholder = (
                "__PZ_RAW_LITERAL_"
                + str(len(result))
                + "_"
                + hashlib.sha256(match[0].encode()).hexdigest()[:12]
                + "__"
            )
            result.append((match.start(), match.end(), placeholder, match[0]))
    return result


def restore_raw_literals(text, original):
    for _, _, placeholder, value in raw_literals(original):
        text = text.replace(literal(placeholder), value)
    if "__PZ_RAW_LITERAL_" in text:
        raise FormatError("Неизвестный маркер литерала Lua")
    return text


def literal_table(text):
    try:
        return LuaTable(text)
    except FormatError:
        return None
