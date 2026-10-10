"""Offline Piper speech and Whisper forced alignment, with local model caches."""

from __future__ import annotations

import io
import hashlib
import json
import math
import re
import time
import os
import threading
import urllib.error
import urllib.request
import wave
from contextlib import contextmanager
from pathlib import Path

from audio_errors import ConversionCancelled, SynthesisError

ROOT = Path(__file__).resolve().parent
MODEL_DIR = ROOT / ".app-data" / "models"
DEFAULT_MODEL = "piper-local-v1"
DEFAULT_VOICE = "en_US-lessac-medium"
ALIGNMENT_MODEL = "base.en"
ALIGNMENT_ENGINE = "stable-ts-base-en-v1"
ALIGNMENT_SHA256 = "25a8566e1d0c1e2231d1c762132cd20e0f96a85d16145c3a00adf5d1ac670ead"
ALIGNMENT_URL = f"https://openaipublic.azureedge.net/main/whisper/models/{ALIGNMENT_SHA256}/{ALIGNMENT_MODEL}.pt"
VOICES = (DEFAULT_VOICE, "en_US-amy-medium", "en_US-ryan-medium")
VOICE_LABELS = {DEFAULT_VOICE: "Lessac · Clear narrator", "en_US-amy-medium": "Amy · Gentle narrator", "en_US-ryan-medium": "Ryan · Steady narrator"}
_voice_cache = {}
_voice_lock = threading.Lock()
_alignment_lock = threading.Lock()
_alignment_model = None


def _check(cancelled):
    if cancelled and cancelled():
        raise ConversionCancelled("Recording cancelled. Completed segments remain saved.")


@contextmanager
def _open_download(request, timeout):
    import requests

    class Reader:
        def __init__(self, response):
            self.headers = response.headers
            self.chunks = response.iter_content(chunk_size=8192)

        def read(self, size):
            try:
                return next(self.chunks, b"")
            except requests.RequestException as exc:
                raise OSError("Model download connection interrupted") from exc

    with requests.get(request.full_url, headers=dict(request.header_items()), stream=True, timeout=(15, timeout)) as response:
        response.raise_for_status()
        yield Reader(response)


def _download(url: str, path: Path, cancelled=None) -> None:
    """Download public weights in bounded ranges, retrying incomplete transfers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    cursor, total, retries = 0, None, 0
    try:
        with partial.open("wb") as target:
            while total is None or cursor < total:
                _check(cancelled)
                end = cursor + 4 * 1024 * 1024 - 1
                request = urllib.request.Request(url, headers={"User-Agent":"Tovoice-local/1.0", "Range":f"bytes={cursor}-{end}"})
                try:
                    with _open_download(request, timeout=60) as response:
                        content_range = response.headers.get("Content-Range", "")
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                        if match:
                            if int(match[1]) != cursor:
                                raise SynthesisError("The model server returned an unexpected byte range.")
                            expected_end, total = int(match[2]) + 1, int(match[3])
                        else:
                            if cursor:
                                raise SynthesisError("The model server could not resume this transfer. Retry its download.")
                            length = int(response.headers.get("Content-Length", 0))
                            total = length or None
                            expected_end = length or None
                        started = cursor
                        while data := response.read(8192):
                            _check(cancelled)
                            target.write(data)
                            cursor += len(data)
                        if cursor == started or expected_end and cursor != expected_end:
                            raise OSError("Incomplete transfer")
                        retries = 0
                        if total is None:
                            total = cursor
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    retries += 1
                    if retries >= 3:
                        raise SynthesisError("Could not finish the local model download. Connect once to finish setup, then retry.") from exc
                    time.sleep(retries)
        _check(cancelled)
        for attempt in range(6):
            try:
                partial.replace(path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.03 * 2 ** attempt)
    finally:
        partial.unlink(missing_ok=True)


def voice_paths(voice: str) -> tuple[Path, Path]:
    if voice not in VOICES:
        raise SynthesisError("Choose an available local voice.")
    folder = MODEL_DIR / "piper"
    return folder / f"{voice}.onnx", folder / f"{voice}.onnx.json"


def ensure_voice(voice: str, cancelled=None, progress=lambda message: None) -> Path:
    model, config = voice_paths(voice)
    language, region, speaker, quality = voice.split("_")[0], voice.split("-")[0], *voice.split("-")[1:]
    base = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/{language}/{region}/{speaker}/{quality}/{voice}"
    for path, suffix in ((config, ".onnx.json"), (model, ".onnx")):
        _check(cancelled)
        if not path.is_file() or path.stat().st_size == 0:
            progress(f"Downloading the {VOICE_LABELS[voice].split(' · ')[0]} voice for local use")
            _download(base + suffix, path, cancelled)
    try:
        json.loads(config.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise SynthesisError("The local voice configuration is invalid. Remove its cached model files and download again.") from exc
    return model


def synthesize(text: str, voice: str = DEFAULT_VOICE, *, cancelled=None, progress=lambda message: None) -> bytes:
    _check(cancelled)
    try:
        from piper import PiperVoice
        from piper.config import SynthesisConfig
    except ImportError as exc:
        raise SynthesisError("Local speech libraries are missing. Run the Tovoice launcher to finish setup.") from exc
    with _voice_lock:
        if voice not in _voice_cache:
            model_path = ensure_voice(voice, cancelled, progress)
            progress("Loading the local speech model")
            _voice_cache[voice] = PiperVoice.load(str(model_path), use_cuda=False)
        model = _voice_cache[voice]
        _check(cancelled)
        result = io.BytesIO()
        with wave.open(result, "wb") as output:
            configured = False
            for chunk in model.synthesize(text, syn_config=SynthesisConfig(length_scale=1.05)):
                _check(cancelled)
                if not configured:
                    output.setframerate(chunk.sample_rate)
                    output.setsampwidth(chunk.sample_width)
                    output.setnchannels(chunk.sample_channels)
                    configured = True
                output.writeframes(chunk.audio_int16_bytes)
            if not configured:
                raise SynthesisError("The local voice returned no audio.")
        return result.getvalue()


def _load_alignment(progress=lambda message: None):
    global _alignment_model
    if _alignment_model is None:
        try:
            import torch
            import stable_whisper
        except ImportError as exc:
            raise SynthesisError("Local alignment libraries are missing. Run the Tovoice launcher to finish setup.") from exc
        torch.set_num_threads(min(4, os.cpu_count() or 1))
        folder = MODEL_DIR / "whisper"
        ensure_alignment_model(progress)
        progress("Loading the local word-alignment model")
        _alignment_model = stable_whisper.load_model(ALIGNMENT_MODEL, device="cpu", download_root=str(folder))
    return _alignment_model


def ensure_alignment_model(progress=lambda message: None) -> Path:
    path = MODEL_DIR / "whisper" / f"{ALIGNMENT_MODEL}.pt"

    def valid():
        if not path.is_file():
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == ALIGNMENT_SHA256

    if not valid():
        progress("Downloading the local word-alignment model for offline use")
        _download(ALIGNMENT_URL, path)
        if not valid():
            raise SynthesisError("The alignment model checksum did not match. Retry its download.")
    return path


def align_words(audio: bytes, text: str, *, progress=lambda message: None) -> list[dict]:
    """Align the known narration locally, without an audio upload or ASR request."""
    import numpy as np
    from scipy.signal import resample_poly
    with wave.open(io.BytesIO(audio), "rb") as source:
        if source.getsampwidth() != 2:
            raise SynthesisError("Local alignment expects 16-bit PCM WAV audio.")
        rate, channels = source.getframerate(), source.getnchannels()
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        divisor = math.gcd(rate, 16000)
        samples = resample_poly(samples, 16000 // divisor, rate // divisor).astype(np.float32)
    with _alignment_lock:
        model = _load_alignment(progress)
        result = model.align(samples, text, language="en", verbose=None, regroup=False, vad=False,
                             suppress_silence=True)
    if result is None:
        raise SynthesisError("Local word alignment failed. Audio playback is still available.")
    words = [{"text": word.word.strip(), "start": float(word.start), "end": float(word.end)}
             for segment in result.segments for word in segment.words or []
             if word.end > word.start and word.word.strip()]
    if not words:
        raise SynthesisError("Local alignment returned no word timings. Audio playback is still available.")
    return words


def models_ready() -> bool:
    return all(path.is_file() for path in voice_paths(DEFAULT_VOICE)) and (MODEL_DIR / "whisper" / f"{ALIGNMENT_MODEL}.pt").is_file()


def prepare_models() -> None:
    ensure_voice(DEFAULT_VOICE, progress=lambda message: print(message, flush=True))
    ensure_alignment_model(lambda message: print(message, flush=True))
    for voice in VOICES[1:]:
        ensure_voice(voice, progress=lambda message: print(message, flush=True))
    print("Local speech and alignment models are ready.", flush=True)


if __name__ == "__main__":
    prepare_models()
