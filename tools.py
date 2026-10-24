"""Optional tools the assistant can call on its own: page reading and emotion.

Tools are disabled or enabled by environment variables (see the README).
Results are plain text because one small local model reads them;
JSON adds noise and the model is constrained to short spoken replies.

Browser tools share an isolated Playwright MCP session per conversation.
Weather uses Open-Meteo's fixed endpoints, so a plain "¿qué tiempo hace?" never
depends on a search engine. Failures are explicit; a blocked page is never
treated as a search result.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from browser_tool import BrowseError
from mcp_browser import MCPBrowserSession, browser_specs, discover_tools
from weather_tool import WeatherError, weather as lookup_weather

EMOTIONS = ("smile", "sad", "surprise", "ask", "angry", "laugh")

# The schema constrains the enum, but this model still reaches for nearby
# words ("happy", "curious"); map those onto the six expressions.
EMOTION_ALIASES = {
    "happy": "smile",
    "joy": "smile",
    "glad": "smile",
    "pleased": "smile",
    "funny": "laugh",
    "amused": "laugh",
    "lol": "laugh",
    "curious": "ask",
    "confused": "ask",
    "question": "ask",
    "thinking": "ask",
    "surprised": "surprise",
    "shock": "surprise",
    "shocked": "surprise",
    "sadness": "sad",
    "unhappy": "sad",
    "upset": "sad",
    "sorry": "sad",
    "mad": "angry",
    "frustrated": "angry",
    "annoyed": "angry",
}

# The system prompt is Spanish, so the model sometimes names the expression in
# Spanish (or leaks the bare word instead of the call syntax).
SPANISH_ALIASES = {
    "feliz": "smile",
    "alegre": "smile",
    "contento": "smile",
    "contenta": "smile",
    "triste": "sad",
    "tristeza": "sad",
    "sorpresa": "surprise",
    "sorprendido": "surprise",
    "sorprendida": "surprise",
    "curioso": "ask",
    "curiosa": "ask",
    "duda": "ask",
    "enfadado": "angry",
    "enfadada": "angry",
    "enojado": "angry",
    "enojada": "angry",
    "molesto": "angry",
    "molesta": "angry",
    "risa": "laugh",
    "gracioso": "laugh",
    "graciosa": "laugh",
    "divertido": "laugh",
    "divertida": "laugh",
}
EMOTION_ALIASES.update(SPANISH_ALIASES)

# Tools whose result the model needs before it can answer. A call to one of
# these suppresses the rest of that message: the model tends to answer from
# memory right after the call, and that guess must not be spoken.
BLOCKING_TOOLS = {"browse", "navigate", "browser_click", "browser_type", "browser_snapshot", "browser_close", "weather", "current_time"}

_QUOTE_TOKEN = '<|"|>'
_LEADING_EMOTION_RE = re.compile(
    "(" + "|".join(EMOTIONS + tuple(SPANISH_ALIASES)) + r")\b", re.IGNORECASE
)
_INLINE_ARGS_RE = re.compile(
    r"([A-Za-z_]\w*)\s*:\s*"
    r'(?:"((?:[^"\\]|\\.)*)"|\'([^\']*)\'|([^,}]+))',
    re.DOTALL,
)


class ToolError(Exception):
    """A caller-visible tool failure; returned to the model, never raised."""


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def tools_enabled() -> bool:
    return _flag("DOT_TOOLS", True)


def tool_specs() -> list[dict[str, Any]]:
    """OpenAI-style function definitions for the tools that are enabled."""
    if not tools_enabled():
        return []

    specs: list[dict[str, Any]] = [
        {"type": "function", "function": {
            "name": "current_time",
            "description": "Consulta la fecha y hora actuales del reloj local en España (Europe/Madrid). Úsala para hoy, la fecha o la hora actual; no las adivines.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }},
        {
            "type": "function",
            "function": {
                "name": "emotion",
                "description": (
                    "Cambia brevemente la expresión del punto para matizar lo "
                    "que dices. Úsala como máximo una vez por respuesta y solo "
                    "cuando el tono sea evidente."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": list(EMOTIONS),
                            "description": "La expresión que se mostrará.",
                        }
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "weather",
                "description": (
                    "Consulta el tiempo actual y la previsión de hoy, mañana y "
                    "pasado mañana para un lugar. Úsala para cualquier pregunta "
                    "sobre el tiempo en vez de abrir un buscador."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "place": {
                            "type": "string",
                            "description": "Ciudad o lugar, por ejemplo Madrid o Valencia, España.",
                        }
                    },
                    "required": ["place"],
                    "additionalProperties": False,
                },
            },
        },
    ]

    # Honor the former switch for existing installations; DOT_BROWSE wins.
    if _flag("DOT_BROWSE", _flag("DOT_SEARCH", True)):
        url = {"url": {"type": "string", "description": "URL HTTP(S) pública completa."}}
        ref = {"ref": {"type": "string", "description": "Referencia ARIA exacta [ref=eN] del árbol de accesibilidad de la última vista, por ejemplo e12."}}
        navigation = "Abre una URL pública con Chromium y devuelve su árbol de accesibilidad con roles, nombres y referencias ARIA para interactuar. Conserva el estado en ese dominio. Si falla, no inventes datos."
        for name, description, properties in [
            ("browse", navigation + " Equivale a navigate.", url),
            ("navigate", navigation, url),
            ("browser_click", "Pulsa un elemento por su referencia ARIA exacta de la última vista y devuelve el nuevo árbol de accesibilidad. Solo navegación GET/HEAD; no pagos, cuentas ni envíos POST.", ref),
            ("browser_type", "Rellena un campo de la última vista sin pulsar Enter ni enviar el formulario. No contraseñas ni archivos. Devuelve el árbol de accesibilidad actualizado.",
             {**ref, "text": {"type": "string", "maxLength": 2000, "description": "Texto que se escribirá, sustituyendo el contenido anterior."}}),
            ("browser_snapshot", "Vuelve a devolver el árbol de accesibilidad de la vista actual con referencias frescas. Úsala al empezar a hablar de una página ya abierta o cuando necesites pulsar o escribir y no tengas la última vista.", {}),
            ("browser_close", "Cierra el navegador solo si el usuario lo pide expresamente. No la llames al terminar una consulta: la sesión se conserva para las siguientes preguntas. El servidor la cierra al final de la conversación.", {}),
        ]:
            specs.append({
                "type": "function",
                "function": {"name": name, "description": description, "parameters": {
                    "type": "object", "properties": properties,
                    "required": list(properties), "additionalProperties": False,
                }},
            })

        native = browser_specs()
        if native:
            native_names = {spec["function"]["name"] for spec in native}
            specs = [spec for spec in specs if spec["function"]["name"] not in native_names]
            specs.extend(native)
            for spec in native:
                function = spec["function"]
                BLOCKING_TOOLS.add(function["name"])
                _POSITIONAL_ARGS[function["name"]] = tuple(function["parameters"].get("required", ()))

    return specs


async def prepare_tools() -> None:
    if tools_enabled() and _flag("DOT_BROWSE", _flag("DOT_SEARCH", True)):
        await discover_tools()


def _parse_args(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"los argumentos no son JSON válido: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ToolError("los argumentos deben ser un objeto JSON")
    return parsed


def parse_inline_arguments(body: str) -> Optional[dict[str, Any]]:
    """Read the Gemma-style ``key:<|"|>value<|"|>`` argument body.

    Returns None when nothing looks like an argument, so the parser can put the
    fragment back into the reply instead of silently swallowing it.
    """
    text = body.replace(_QUOTE_TOKEN, '"').strip()
    if not text:
        return {}
    try:
        parsed = json.loads("{" + text + "}")
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    args: dict[str, Any] = {}
    for match in _INLINE_ARGS_RE.finditer(text):
        value = match.group(2)
        if value is None:
            value = match.group(3)
        if value is None:
            value = (match.group(4) or "").strip()
        args[match.group(1)] = value
    return args or None


_POSITIONAL_ARGS = {
    "navigate": ("url",), "browse": ("url",),
    "browser_click": ("ref",), "browser_type": ("ref", "text"),
    "browser_snapshot": (), "browser_close": (), "emotion": ("name",), "weather": ("place",), "current_time": (),
}


def parse_parenthesized_arguments(name: str, body: str) -> Optional[dict[str, Any]]:
    """Decode literal call arguments only; never evaluate model-supplied code."""
    text = body.replace(_QUOTE_TOKEN, '"').strip()
    keys = _POSITIONAL_ARGS.get(name.lower(), ())
    # This model often leaves the single positional value unquoted:
    # navigate(https://www.reddit.com), weather(Madrid), emotion(happy).
    # Treat it as the literal string it is; never evaluate it.
    if (
        text
        and len(keys) == 1
        and text[0].isalnum()
        and not any(char in text for char in "{}\"'()")
        and not re.match(r"^(?:" + "|".join(map(re.escape, keys)) + r")\s*[:=]", text, re.I)
    ):
        return {keys[0]: text}
    try:
        call = ast.parse("tool(" + text + ")", mode="eval").body
        if not isinstance(call, ast.Call):
            return None
        # Also accept navigate({"url": "..."}).
        if len(call.args) == 1 and isinstance(call.args[0], ast.Dict) and not call.keywords:
            value = ast.literal_eval(call.args[0])
            return value if set(value) <= set(keys) else None
        if len(call.args) > len(keys):
            return None
        args = {key: ast.literal_eval(value) for key, value in zip(keys, call.args)}
        for keyword in call.keywords:
            if keyword.arg not in keys or keyword.arg in args:
                return None
            args[keyword.arg] = ast.literal_eval(keyword.value)
        return args
    except (SyntaxError, ValueError, TypeError, RecursionError):
        return None


class InlineToolParser:
    """Recover tool calls the model writes as plain text.

    The Nemotron/Gemma template declares tools but never shows the call syntax,
    so this model often emits ``emotion{name:<|"|>smile<|"|>}`` without the
    ``<|tool_call>`` wrapper llama.cpp needs. Feeding streamed text through
    here turns those fragments into the same events as a native call, and keeps
    them out of the spoken reply.

    A fragment can span deltas, so a trailing prefix that might become a call
    is held back until the next chunk (or the final flush) resolves it.
    """

    def __init__(self, names: list[str]) -> None:
        self.names = [name for name in names if name]
        self.buffer = ""
        self.index = 0
        self.started = False
        if self.names:
            alternation = "|".join(re.escape(name) for name in self.names)
            self._start = re.compile(
                r"(?<![A-Za-z0-9_])(?:call:)?(" + alternation + r")\s*([\{(])",
                re.IGNORECASE,
            )
            stems = []
            for name in self.names:
                stems.extend((name.lower(), f"call:{name.lower()}"))
            self._stems = stems
        else:
            self._start = None
            self._stems = []

    def feed(self, text: str, final: bool = False) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if not self.names:
            if text:
                events.append({"type": "text", "text": text})
            return events

        self.buffer += text
        while self.buffer:
            if not self.started:
                leading = self._leading_emotion(final)
                if leading is not None:
                    if not leading:
                        break  # wait for the next word
                    events.extend(leading)
                    continue

            match = self._start.search(self.buffer)
            if match is not None:
                if match.start() > 0:
                    events.append({"type": "text", "text": self.buffer[: match.start()]})
                    self.started = True
                    self.buffer = self.buffer[match.start():]
                    match = self._start.match(self.buffer)
                assert match is not None
                close = self._find_close(match.end(), match.group(2))
                if close < 0:
                    if final:
                        self.buffer = ""
                    break
                body = self.buffer[match.end(): close]
                args = (parse_parenthesized_arguments(match.group(1), body)
                        if match.group(2) == "(" else parse_inline_arguments(body))
                if args is None:
                    # Recognized tool syntax must never reach speech. Dispatch
                    # reports malformed arguments to the model instead.
                    events.append(self._call_event(match.group(1), {"_parse_error": True}))
                else:
                    events.append(self._call_event(match.group(1), args))
                self.buffer = self.buffer[close + 1:]
                continue

            hold = self._hold_len(self.buffer)
            cut = len(self.buffer) - hold
            if cut > 0:
                events.append({"type": "text", "text": self.buffer[:cut]})
                self.started = True
                self.buffer = self.buffer[cut:]
            if final and self.buffer:
                events.append({"type": "text", "text": self.buffer})
                self.started = True
                self.buffer = ""
            break
        return events

    def _leading_emotion(self, final: bool) -> Optional[list[dict[str, Any]]]:
        """Take a bare leading ``smile`` as a call when it starts a sentence.

        This model sometimes drops the braces entirely. Only a lowercase name
        followed by a new sentence (or nothing at all) counts, so "Ask me
        anything" and "Smile!" stay ordinary text.
        """
        match = _LEADING_EMOTION_RE.match(self.buffer)
        if not match:
            return None
        if "emotion" not in {name.lower() for name in self.names}:
            return None
        name = match.group(1)
        if name != name.lower():
            return None
        rest = self.buffer[match.end():].lstrip(" \t\r\n,;:!—–-")
        if not rest:
            if not final:
                return []  # wait for the next word
            self.buffer = ""
            self.started = True
            return [self._call_event("emotion", {"name": name})]
        if not (rest[0].isupper() or rest[0] in "\"'“¿¡"):
            return None
        self.buffer = rest
        self.started = True
        return [self._call_event("emotion", {"name": name})]

    def _call_event(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        event = {
            "type": "tool_call",
            "source": "inline",
            "index": self.index,
            "id": "",
            "name": self._canonical(name),
            "arguments": json.dumps(args),
        }
        self.index += 1
        return event

    def _canonical(self, name: str) -> str:
        lowered = name.lower()
        for candidate in self.names:
            if candidate.lower() == lowered:
                return candidate
        return lowered

    def _find_close(self, start: int, opening: str) -> int:
        """Balance delimiters, respecting quoted strings and escape sequences."""
        stack = ["}" if opening == "{" else ")"]
        quote = None
        index = start
        while index < len(self.buffer):
            if self.buffer.startswith(_QUOTE_TOKEN, index):
                quote = None if quote == _QUOTE_TOKEN else _QUOTE_TOKEN
                index += len(_QUOTE_TOKEN)
                continue
            char = self.buffer[index]
            if quote:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char in "({[":
                stack.append({"(": ")", "{": "}", "[": "]"}[char])
            elif char in ")}]":
                if char != stack[-1]:
                    return index
                stack.pop()
                if not stack:
                    return index
            index += 1
        return -1

    def _hold_len(self, text: str) -> int:
        """Length of a trailing fragment that could still become a call start."""
        if not self._stems:
            return 0
        stems = self._stems
        if not self.started:
            # A reply may open with a bare emotion word; hold its prefix too.
            stems = stems + [name.lower() for name in EMOTIONS + tuple(SPANISH_ALIASES)]
        limit = min(len(text), max(len(stem) for stem in stems) + 8)
        for size in range(limit, 0, -1):
            tail = text[-size:].lower()
            for stem in stems:
                if stem.startswith(tail):
                    return size
                if tail.startswith(stem) and not tail[len(stem):].strip():
                    return size
        return 0


class Toolbox:
    """Executes tool calls for one conversation.

    `emit` is how side effects reach a client; for the voice socket it sends an
    ``emotion`` frame. Calls never raise: failures come back as text the model
    can read and recover from.
    """

    def __init__(
        self,
        emit: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self.emit = emit
        self.browser = MCPBrowserSession()

    async def close(self) -> None:
        await self.browser.close()

    def specs(self) -> list[dict[str, Any]]:
        return tool_specs()

    async def call(self, name: str, arguments: Any = None) -> str:
        name = (name or "").strip()
        try:
            if name not in {spec["function"]["name"] for spec in self.specs()}:
                return f"error: herramienta desconocida o desactivada: {name or '(ninguna)'}"
            args = _parse_args(arguments)
            if "_parse_error" in args:
                return "error: argumentos malformados; vuelve a llamar con valores literales y los parámetros indicados"
            if name == "emotion":
                return await self._emotion(args)
            if name == "current_time":
                now = datetime.now(ZoneInfo("Europe/Madrid"))
                return (now.isoformat(timespec="seconds") + " [Europe/Madrid]\n"
                        + f"Hora exacta para responder (HH:MM): {now:%H:%M}. Fecha: {now:%Y-%m-%d}.")
            if name == "weather":
                return await lookup_weather(args.get("place", ""))
            if name in {"browse", "navigate"}:
                return await self.browser.navigate(args.get("url", ""))
            if name == "browser_click":
                return await self.browser.click(args.get("target", args.get("ref", "")))
            if name == "browser_type":
                if args.get("submit") or args.get("slowly"):
                    raise ToolError("solo rellenado sin enviar formularios")
                return await self.browser.type(args.get("target", args.get("ref", "")), args.get("text", ""))
            if name == "browser_snapshot":
                return await self.browser.snapshot()
            if name == "browser_close":
                return await self.browser.close()
            if name.startswith("browser_"):
                return await self.browser.call(name, args)
            return f"error: herramienta desconocida: {name or '(ninguna)'}"
        except (ToolError, BrowseError, WeatherError) as exc:
            return f"error: {exc}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - model-visible failure
            return f"error: {type(exc).__name__}: {exc}"

    async def _emotion(self, args: dict[str, Any]) -> str:
        name = str(args.get("name", "")).strip().lower()
        name = EMOTION_ALIASES.get(name, name)
        if name not in EMOTIONS:
            return f"error: emoción desconocida: {name or '(ninguna)'}"
        if self.emit is not None:
            await self.emit(name)
        return f"mostrando {name}"
