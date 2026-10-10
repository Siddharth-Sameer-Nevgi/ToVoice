"""Measured audio timestamps mapped to text from the original Markdown."""

from __future__ import annotations

import base64
import difflib
import json
import math
import re
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

from md_to_audio import KeyUnavailableError, QuotaError, SynthesisError, _atomic_write, markdown_blocks, segment_plan

TRANSCRIBE_MODEL = "gemini-3.5-transcribe"
TIMING_VERSION = 1


def seconds(value) -> float:
    number = float(str(value).removesuffix("s"))
    if not math.isfinite(number) or number < 0:
        raise ValueError("Invalid timestamp")
    return number


def parse_timestamps(body: dict) -> list[dict]:
    words = []

    def visit(item):
        if isinstance(item, dict):
            if item.get("type") == "word_info":
                start, end = seconds(item["start_offset"]), seconds(item["end_offset"])
                if end <= start or not isinstance(item.get("text"), str):
                    raise ValueError("Invalid word timestamp")
                words.append({"text": item["text"], "start": start, "end": end})
            else:
                for value in item.values():
                    visit(value)
        elif isinstance(item, list):
            for value in item:
                visit(value)

    visit(body)
    if not words:
        raise SynthesisError("Gemini returned no word timestamps. Audio is still available.")
    return sorted(words, key=lambda word: word["start"])


def transcribe_words(audio: bytes, key: str) -> list[dict]:
    payload = {
        "model": TRANSCRIBE_MODEL, "store": False,
        "input": [{"type": "audio", "data": base64.b64encode(audio).decode("ascii"), "mime_type": "audio/wav"}],
        "generation_config": {"transcription_config": {"mode": {"type": "verbatim", "timestamp_granularities": ["word"]}}},
    }
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key}, method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return parse_timestamps(json.load(response))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise QuotaError("Word timing quota reached (HTTP 429)") from None
            if exc.code in {401, 403}:
                raise KeyUnavailableError(f"This key cannot prepare word timings (HTTP {exc.code})") from None
            if exc.code in {500, 502, 503, 504} and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise SynthesisError(f"Word timing request failed (HTTP {exc.code}). The audio is still available.") from None
        except (urllib.error.URLError, TimeoutError):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise SynthesisError("Cannot connect to Gemini to prepare highlights. The audio is still available.") from None
        except (KeyError, ValueError) as exc:
            raise SynthesisError("Gemini returned invalid word timestamps. The audio is still available.") from exc
    raise SynthesisError("Could not prepare word timings")


def tokens(text: str) -> list[dict]:
    return [{"text": match.group(), "start": match.start(), "end": match.end(),
             "normal": "".join(char for char in match.group().casefold() if char.isalnum())}
            for match in re.finditer(r"\S+", text)]


def _source_word_map(block: dict) -> list[dict]:
    original = [word for word in tokens(block["text"]) if word["normal"]]
    spoken = tokens(block["narration"])
    mappings = []
    matcher = difflib.SequenceMatcher(None, [word["normal"] for word in original], [word["normal"] for word in spoken], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        for j in range(j1, j2):
            if tag == "equal":
                source = original[i1 + j - j1]
                begin, end = source["start"], source["end"]
            elif i2 > i1:
                begin, end = original[i1]["start"], original[i2 - 1]["end"]
            else:
                begin = end = 0
            mappings.append({"start": spoken[j]["start"], "end": spoken[j]["end"], "source_start": begin, "source_end": end})
    return mappings


def _ranges(blocks: list[dict], start: int, end: int) -> list[dict]:
    ranges = []
    for block in blocks:
        if block["flat_end"] <= start or block["flat_start"] >= end:
            continue
        local_start = max(0, start - block["flat_start"])
        local_end = min(len(block["narration"]), end - block["flat_start"])
        mapped = [word for word in block["word_map"] if word["start"] < local_end and word["end"] > local_start and word["source_end"] > word["source_start"]]
        ranges.append({"block": block["id"], "narration_start": local_start, "narration_end": local_end,
                       "source_start": min((word["source_start"] for word in mapped), default=0),
                       "source_end": max((word["source_end"] for word in mapped), default=0)})
    return ranges


def map_word_cues(text: str, words: list[dict], blocks: list[dict], offset: int, audio_offset: float, duration: float) -> list[dict]:
    source = tokens(text)
    matcher = difflib.SequenceMatcher(None, [word["normal"] for word in source],
                                    ["".join(char for char in word["text"].casefold() if char.isalnum()) for word in words], autojunk=False)
    opcodes = matcher.get_opcodes()
    matches = sum(i2 - i1 for tag, i1, i2, _, _ in opcodes if tag == "equal")
    if not source or matches / len(source) < 0.35:
        raise SynthesisError("The spoken words could not be matched reliably to the Markdown. Segment highlighting is still available.")
    cues = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            groups = [(i, i + 1, j1 + i - i1, j1 + i - i1 + 1) for i in range(i1, i2)]
        elif tag == "replace" and 0 < i2 - i1 <= 6 and 0 < j2 - j1 <= 10:
            groups = [(i1, i2, j1, j2)]
        else:
            continue
        for a, b, c, d in groups:
            begin, end = max(0, words[c]["start"]), min(duration, words[d - 1]["end"])
            if end <= begin:
                continue
            cues.append({"start": audio_offset + begin, "end": audio_offset + end,
                         "kind": "word" if b - a == 1 else "phrase",
                         "ranges": _ranges(blocks, offset + source[a]["start"], offset + source[b - 1]["end"])})
    return cues


def _cached_words(path: Path) -> list[dict] | None:
    try:
        cached = json.loads(path.with_suffix(".words.json").read_text(encoding="utf-8"))
        stat = path.stat()
        if cached["version"] == TIMING_VERSION and cached["size"] == stat.st_size and cached["mtime"] == stat.st_mtime_ns:
            words = cached["words"]
            if not words or any(not isinstance(word.get("text"), str) or seconds(word["end"]) <= seconds(word["start"]) for word in words):
                return None
            return words
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def document(source: str, output: Path, model: str, voice: str) -> dict:
    blocks = markdown_blocks(source)
    cursor = 0
    for block in blocks:
        block.update(flat_start=cursor, flat_end=cursor + len(block["narration"]), word_map=_source_word_map(block))
        cursor = block["flat_end"] + 1
    flat = " ".join(block["narration"] for block in blocks)
    narration = "\n\n".join(block["narration"] for block in blocks)
    chunks, files = segment_plan(narration, output, model, voice)
    cues = []
    elapsed, cursor, available, aligned = 0.0, 0, 0, 0
    for chunk, path in zip(chunks, files):
        if not path.exists():
            break
        with wave.open(str(path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
        start = flat.find(chunk, cursor)
        if start < 0:
            raise SynthesisError("The audio segments do not match the saved narration.")
        cursor = start + len(chunk)
        words = _cached_words(path)
        if words:
            try:
                word_cues = map_word_cues(chunk, words, blocks, start, elapsed, duration)
                if not word_cues:
                    raise SynthesisError("No matching word timings")
                cues.extend(word_cues)
                aligned += 1
            except SynthesisError:
                words = None
        if not words:
            cues.append({"start": elapsed, "end": elapsed + duration, "kind": "segment", "ranges": _ranges(blocks, start, cursor)})
        elapsed += duration
        available += 1
    for block in blocks:
        block.pop("word_map")
        block.pop("flat_start")
        block.pop("flat_end")
    return {"blocks": blocks, "cues": sorted(cues, key=lambda cue: cue["start"]), "duration": elapsed,
            "precision": "word" if aligned == len(chunks) and aligned else "mixed" if aligned else "segment",
            "aligned_segments": aligned, "available_segments": available, "total_segments": len(chunks)}


def prepare(source: str, output: Path, model: str, voice: str, keys: list[str], transcriber=transcribe_words, progress=lambda value: None) -> None:
    narration = "\n\n".join(block["narration"] for block in markdown_blocks(source))
    chunks, files = segment_plan(narration, output, model, voice)
    unavailable = set()
    key_index = 0
    for number, (chunk, path) in enumerate(zip(chunks, files), 1):
        if not path.exists():
            raise SynthesisError("Saved audio segments are missing. The finished recording is still available.")
        if not _cached_words(path):
            while len(unavailable) < len(keys):
                if key_index in unavailable:
                    key_index = (key_index + 1) % len(keys)
                    continue
                try:
                    words = transcriber(path.read_bytes(), keys[key_index])
                    with wave.open(str(path), "rb") as audio:
                        duration = audio.getnframes() / audio.getframerate()
                    # Validate timing shape and recognition before saving it.
                    for word in words:
                        if seconds(word["end"]) <= seconds(word["start"]):
                            raise SynthesisError("Invalid word timings")
                    probe = [{"id": 0, "text": chunk, "narration": chunk, "flat_start": 0, "flat_end": len(chunk)}]
                    probe[0]["word_map"] = _source_word_map(probe[0])
                    map_word_cues(chunk, words, probe, 0, 0, duration)
                    stat = path.stat()
                    _atomic_write(path.with_suffix(".words.json"), json.dumps({"version": TIMING_VERSION, "size": stat.st_size,
                                  "mtime": stat.st_mtime_ns, "words": words}).encode())
                    break
                except (QuotaError, KeyUnavailableError):
                    unavailable.add(key_index)
                    key_index = (key_index + 1) % len(keys)
            else:
                raise SynthesisError("All keys are unavailable for word timings. Play the audio now or retry highlights later; saved timings will be reused.")
        progress(number)
