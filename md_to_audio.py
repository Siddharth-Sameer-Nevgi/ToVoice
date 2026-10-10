"""Convert Markdown prose to a resumable WAV recording with Gemini TTS."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from markdown_it import MarkdownIt


DEFAULT_MODEL = "gemini-3.8-flash-lite-tts"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
SPEECH_STYLE = "clear, conversational explanation"


class QuotaError(Exception):
    """The current API key's project is rate limited or out of quota."""


class KeyUnavailableError(Exception):
    """The current API key cannot access the model."""


class SynthesisError(Exception):
    """Audio generation failed for a reason key rotation cannot resolve."""


class ConversionCancelled(SynthesisError):
    """The user stopped generation; completed audio segments remain saved."""


def _spoken_code(value: str) -> str:
    """Retain useful inline names without making the voice read code punctuation."""
    value = value.strip()
    if re.search(r"https?://|www\.", value, flags=re.I):
        return "the linked page"
    if value.startswith("@default("):
        return "default " + _humanize_identifier(value[9:-1].strip("\"'")).replace("/", " ")
    if re.search(r"[\\/]", value) and re.search(r"\.[A-Za-z0-9]{1,8}(?:\b|$)", value):
        name = re.split(r"[\\/]", value)[-1].rsplit(".", 1)[0]
        return f"the {_humanize_identifier(name.replace('.', ' '))} file"
    if re.search(r"[\\/]", value) and "@" in value:
        return "the source repository"
    if value.startswith(("/", "\\")) and not re.search(r"\.[A-Za-z0-9]{1,8}(?:\b|$)", value):
        return value.strip("/\\")
    if re.fullmatch(r"[\w.-]+\.(?:ts|tsx|js|jsx|py|prisma|sql|md|json)", value, flags=re.I):
        return f"the {_humanize_identifier(value.rsplit('.', 1)[0].replace('.', ' '))} file"
    if value.endswith("[]"):
        return "array of " + _humanize_identifier(value[:-2]).lower() + "s"
    if value.endswith("()"):
        return ""
    if value.startswith("@@unique"):
        return "a unique constraint"
    if value.startswith("MAX(") and value.endswith(")"):
        return "maximum " + _humanize_identifier(value[4:-1])
    if len(value) > 100 or any(char in value for char in "{};$<>"):
        return ""
    return _humanize_identifier(value).replace("?", " optional")


def _humanize_identifier(value: str) -> str:
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return value.replace("_", " ")


def _speech_clean(value: str) -> str:
    """Remove notation the voice would otherwise spell as symbols."""
    value = re.sub(r"https?://\S+|www\.\S+", "the linked page", value)
    replacements = {
        "§": " section ", "→": " then ", "←": " from ",
        "⇒": " then ", "×": " times ", "≈": " approximately ",
        "≥": " at least ", "≤": " at most ", "≠": " is not ",
        "∈": " in ", "·": ", ", "—": ", ", "–": " to ",
        "&": " and ", "+": " and ", "%": " percent ",
    }
    for symbol, spoken in replacements.items():
        value = value.replace(symbol, spoken)
    value = re.sub(r"(?<=\w)\s*/\s*(?=\w)", " or ", value)
    value = value.replace("/", " ").replace("\\", " ")
    value = re.sub(r"[#$@*_|~^=<>\[\]{}]", " ", value)
    value = value.replace("(", " ").replace(")", " ")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"(?:\s*[,;:]\s*){2,}", ", ", value)
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return value.strip(" ,;:-")


def _inline_text(token) -> str:
    pieces = []
    for child in token.children or []:
        if child.type == "text":
            pieces.append(child.content)
        elif child.type == "code_inline":
            pieces.append(_spoken_code(child.content))
        elif child.type in {"softbreak", "hardbreak"}:
            pieces.append(" ")
    return _speech_clean("".join(pieces))


def markdown_blocks(source: str) -> list[dict]:
    """Pair source Markdown text with its spoken form and original line numbers."""
    front = re.match(r"\A\ufeff?---\s*\n.*?\n---\s*(?:\n|\Z)", source, flags=re.S)
    line_offset = source[:front.end()].count("\n") if front else 0
    source = source[front.end():] if front else source
    parser = MarkdownIt("commonmark").enable("table")
    blocks: list[dict] = []
    in_table = False
    in_header = False
    headers: list[str] = []
    row: list[str] = []
    display_row: list[str] = []
    kind, level, quote_depth, list_depth = "paragraph", 0, 0, 0
    lines = [1, 1]

    def append(text: str, display: str, block_kind: str, block_level: int = 0) -> None:
        if text and re.search(r"\w", text):
            blocks.append({"id": len(blocks), "text": display or text, "narration": text,
                           "kind": block_kind, "level": block_level,
                           "line_start": lines[0] + line_offset, "line_end": lines[1] + line_offset})

    for token in parser.parse(source):
        if token.type == "heading_open":
            kind, level = "heading", int(token.tag[1:])
        elif token.type == "heading_close":
            kind, level = "paragraph", 0
        elif token.type == "blockquote_open":
            quote_depth += 1
        elif token.type == "blockquote_close":
            quote_depth -= 1
        elif token.type in {"bullet_list_open", "ordered_list_open"}:
            list_depth += 1
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            list_depth -= 1
        elif token.type == "table_open":
            in_table = True
        elif token.type == "table_close":
            in_table = False
        elif token.type == "thead_open":
            in_header = True
        elif token.type == "thead_close":
            in_header = False
        elif token.type == "tr_open":
            row = []
            display_row = []
        elif token.type == "tr_close" and in_table:
            if in_header:
                headers = row
            else:
                cells = [f"{headers[i]}: {cell}" if i < len(headers) and headers[i] else cell
                         for i, cell in enumerate(row) if cell]
                if cells:
                    append(". ".join(cells).rstrip(".") + ".", " | ".join(display_row), "table")
        elif token.type != "inline":
            continue
        else:
            block = _inline_text(token)
            display = re.sub(r"\s+", " ", "".join(
                child.content if child.type in {"text", "code_inline"} else " "
                if child.type in {"softbreak", "hardbreak"} else "" for child in token.children or []
            )).strip()
            if token.map:
                lines = [token.map[0] + 1, token.map[1]]
            if in_table:
                row.append(block)
                display_row.append(display)
            elif block and re.search(r"\w", block):
                append(block, display, kind if kind == "heading" else "list" if list_depth else "quote" if quote_depth else "paragraph", level)
    return blocks


def markdown_text(source: str) -> str:
    """Keep readable prose while making inline notation speak naturally."""
    return "\n\n".join(block["narration"] for block in markdown_blocks(source))


def segment_plan(text: str, output_path: Path, model: str, voice: str, max_chars: int = 1500) -> tuple[list[str], list[Path]]:
    chunks = split_chunks(text, max_chars)
    job = json.dumps({"chunks": chunks, "model": model, "voice": voice, "style": SPEECH_STYLE}, ensure_ascii=False)
    job_id = hashlib.sha256(job.encode("utf-8")).hexdigest()[:16]
    parts_dir = output_path.parent / f".{output_path.name}.parts" / job_id
    return chunks, [parts_dir / f"{i:05d}.wav" for i in range(1, len(chunks) + 1)]


def split_chunks(text: str, max_chars: int) -> list[str]:
    """Split at paragraph/sentence/word boundaries without losing any text."""
    if max_chars < 100:
        raise ValueError("--max-chars must be at least 100")
    units: list[str] = []
    for paragraph in text.split("\n\n"):
        units.extend(x for x in re.split(r"(?<=[.!?])\s+", paragraph.strip()) if x)

    chunks: list[str] = []
    current = ""

    def add(part: str) -> None:
        nonlocal current
        if not part:
            return
        candidate = f"{current} {part}" if current else part
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = part

    for unit in units:
        if len(unit) <= max_chars:
            add(unit)
            continue
        for word in unit.split():
            if len(word) <= max_chars:
                add(word)
            else:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(word[i : i + max_chars] for i in range(0, len(word), max_chars))
    if current:
        chunks.append(current)
    return chunks


def _wav_params(data: bytes) -> tuple[wave._wave_params, bytes]:
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            params = reader.getparams()
            frames = reader.readframes(params.nframes)
    except (EOFError, wave.Error) as exc:
        raise SynthesisError("Gemini returned invalid WAV audio") from exc
    if not frames or params.comptype != "NONE":
        raise SynthesisError("Gemini returned empty or compressed WAV audio")
    return params, frames


def synthesize(text: str, key: str, model: str, voice: str, timeout: int = 120,
               cancelled: Callable[[], bool] | None = None) -> bytes:
    """Call Gemini's unary TTS endpoint and return a complete WAV file."""
    payload = {
        "contents": [{"role": "user", "parts": [{
            "text": text,
            "speech_metadata": {"style": SPEECH_STYLE},
        }]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "responseFormat": {"audio": {"mimeType": "AUDIO_WAV"}},
            "speechConfig": {"voiceConfig": {"voice": voice}},
        },
    }
    request = urllib.request.Request(
        f"{API_ROOT}/{model}:generateContent",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    for attempt in range(3):
        if cancelled and cancelled():
            raise ConversionCancelled("Recording cancelled. Completed segments remain saved.")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.load(response)
            parts = body["candidates"][0]["content"]["parts"]
            audio = next(part["inlineData"]["data"] for part in parts if "inlineData" in part)
            data = base64.b64decode(audio, validate=True)
            _wav_params(data)
            return data
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise QuotaError("Gemini quota or rate limit reached (HTTP 429)") from None
            if exc.code in (401, 403):
                raise KeyUnavailableError(f"Gemini rejected this key (HTTP {exc.code})") from None
            if exc.code in (500, 502, 503, 504) and attempt < 2:
                if cancelled and cancelled():
                    raise ConversionCancelled("Recording cancelled. Completed segments remain saved.") from None
                time.sleep(2 ** attempt)
                continue
            raise SynthesisError(f"Gemini request failed (HTTP {exc.code})") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if cancelled and cancelled():
                raise ConversionCancelled("Recording cancelled. Completed segments remain saved.") from None
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise SynthesisError(f"Network error: {exc.reason if hasattr(exc, 'reason') else exc}") from None
        except (KeyError, IndexError, StopIteration, ValueError) as exc:
            raise SynthesisError("Gemini response did not contain valid WAV audio") from exc
    raise SynthesisError("Gemini request failed after retries")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tmp-", delete=False) as temp:
        temp.write(data)
        temp_path = Path(temp.name)
    try:
        for attempt in range(6):
            try:
                temp_path.replace(path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                # Windows scanners can briefly lock freshly written files.
                time.sleep(0.03 * (2 ** attempt))
    finally:
        temp_path.unlink(missing_ok=True)


def _join_wavs(files: list[Path], output: Path, check_cancel: Callable[[], None] = lambda: None) -> None:
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as writer:
            expected = None
            for path in files:
                check_cancel()
                params, frames = _wav_params(path.read_bytes())
                format_params = (params.nchannels, params.sampwidth, params.framerate, params.comptype)
                if expected is None:
                    expected = format_params
                    writer.setnchannels(params.nchannels)
                    writer.setsampwidth(params.sampwidth)
                    writer.setframerate(params.framerate)
                elif format_params != expected:
                    raise SynthesisError("Audio segments have incompatible WAV formats")
                writer.writeframes(frames)
        check_cancel()
        _atomic_write(output, buffer.getvalue())


def convert(
    input_path: Path,
    output_path: Path,
    keys: list[str],
    *,
    model: str = DEFAULT_MODEL,
    voice: str = "Kore",
    max_chars: int = 1500,
    generator: Callable[[str, str, str, str], bytes] = synthesize,
    progress: Callable[[dict], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    def check_cancel() -> None:
        if cancelled and cancelled():
            raise ConversionCancelled("Recording cancelled. Completed segments remain saved.")

    def report(event: str, message: str, **details) -> None:
        if progress:
            progress({"event": event, "message": message, **details})
        else:
            print(message)

    if not keys:
        raise ValueError("No Gemini API keys configured")
    if input_path.suffix.lower() != ".md":
        raise ValueError("Input must be a .md file")
    if output_path.suffix.lower() != ".wav":
        raise ValueError("Output must be a .wav file")
    text = markdown_text(input_path.read_text(encoding="utf-8"))
    chunks, files = segment_plan(text, output_path, model, voice, max_chars)
    if not chunks:
        raise ValueError("No readable text found in the Markdown file")
    files[0].parent.mkdir(parents=True, exist_ok=True)
    key_index = 0
    unavailable: set[int] = set()
    report("planned", f"Preparing {len(chunks)} audio segments", total=len(chunks), completed=0)

    for number, (chunk, path) in enumerate(zip(chunks, files), 1):
        check_cancel()
        if path.exists():
            try:
                _wav_params(path.read_bytes())
                report("saved", f"[{number}/{len(chunks)}] Reusing saved segment", completed=number, total=len(chunks))
                continue
            except SynthesisError:
                path.unlink()
        while len(unavailable) < len(keys):
            check_cancel()
            if key_index in unavailable:
                key_index = (key_index + 1) % len(keys)
                continue
            report("generating", f"[{number}/{len(chunks)}] Generating audio", segment=number, total=len(chunks), key=key_index + 1)
            try:
                try:
                    if generator is synthesize:
                        data = generator(chunk, keys[key_index], model, voice, cancelled=cancelled)
                    else:
                        data = generator(chunk, keys[key_index], model, voice)
                except Exception:
                    check_cancel()
                    raise
                _wav_params(data)
                _atomic_write(path, data)
                report("saved", f"[{number}/{len(chunks)}] Segment saved", completed=number, total=len(chunks))
                check_cancel()
                break
            except (QuotaError, KeyUnavailableError) as exc:
                report("key_switch", f"Key {key_index + 1}: {exc}; switching keys", key=key_index + 1)
                unavailable.add(key_index)
                key_index = (key_index + 1) % len(keys)
        else:
            raise SynthesisError(
                "All configured keys are unavailable. Saved segments remain; rerun later to resume."
            )

    check_cancel()
    report("assembling", "Combining saved segments into your recording", completed=len(chunks), total=len(chunks))
    _join_wavs(files, output_path, check_cancel)
    check_cancel()
    report("complete", f"Saved {output_path}", completed=len(chunks), total=len(chunks))
    return output_path


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Convert Markdown text to a local WAV recording")
    parser.add_argument("input", type=Path, help="Markdown file to narrate")
    parser.add_argument("-o", "--output", type=Path, help="Output WAV path (default: beside input)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini TTS model")
    parser.add_argument("--voice", default="Kore", help="Gemini voice name")
    parser.add_argument("--max-chars", type=int, default=1500, help="Maximum characters per segment")
    parser.add_argument("--preview-text", action="store_true", help="Print narration text without calling Gemini")
    args = parser.parse_args(argv)
    if args.preview_text:
        try:
            if args.input.suffix.lower() != ".md":
                raise ValueError("Input must be a .md file")
            narration = markdown_text(args.input.read_text(encoding="utf-8"))
            if not narration:
                raise ValueError("No readable text found in the Markdown file")
            sys.stdout.reconfigure(encoding="utf-8")
            print(narration)
            return 0
        except (OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    keys = [key.strip() for key in os.getenv("GEMINI_API_KEYS", "").split(",") if key.strip()]
    if not keys and os.getenv("GEMINI_API_KEY"):
        keys = [os.environ["GEMINI_API_KEY"].strip()]
    output = args.output or args.input.with_suffix(".wav")
    try:
        convert(args.input, output, keys, model=args.model, voice=args.voice, max_chars=args.max_chars)
    except (OSError, ValueError, SynthesisError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
