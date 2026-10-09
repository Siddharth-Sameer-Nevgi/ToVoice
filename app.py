"""Local Tovoice web application. All uploads and recordings stay on disk."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from md_to_audio import ConversionCancelled, DEFAULT_MODEL, SPEECH_STYLE, _atomic_write, convert, markdown_text, split_chunks, synthesize


ROOT = Path(__file__).resolve().parent
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
VOICES = ("Kore", "Puck", "Aoede", "Charon", "Fenrir", "Leda", "Orus", "Zephyr")


def configured_keys() -> list[str]:
    config = {**dotenv_values(ROOT / ".env"), **os.environ}
    raw = config.get("GEMINI_API_KEYS") or config.get("GEMINI_API_KEY") or ""
    return list(dict.fromkeys(key.strip() for key in raw.split(",") if key.strip()))


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, directory: Path, key_loader=configured_keys, generator=synthesize):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.key_loader = key_loader
        self.generator = generator
        self.lock = threading.RLock()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tovoice")
        self.jobs: dict[str, dict] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        for path in directory.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job["state"] in {"running", "queued"}:
                    job.update(state="interrupted", message="Server restarted. Resume to continue saved segments.")
                elif job["state"] == "cancelling":
                    job.update(state="cancelled", error=None, message="Recording cancelled. Saved segments are kept for resuming.")
                self.jobs[job["id"]] = job
            except (OSError, ValueError, KeyError):
                logging.warning("Could not load job metadata from %s", path)

    def _save(self, job: dict) -> None:
        _atomic_write(self.directory / job["id"] / "job.json", json.dumps(job, ensure_ascii=False).encode("utf-8"))

    def get(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError("Recording not found")
            return dict(self.jobs[job_id])

    def list(self) -> list[dict]:
        with self.lock:
            return [dict(job) for job in sorted(self.jobs.values(), key=lambda item: item["created_at"], reverse=True)]

    def create(self, filename: str, source: str, voice: str = "Kore") -> dict:
        filename = re.split(r"[\\/]", filename)[-1]
        if not filename.lower().endswith(".md"):
            raise ValueError("Choose a Markdown (.md) file.")
        if not source.strip():
            raise ValueError("This file is empty.")
        if len(source.encode("utf-8")) > MAX_UPLOAD_BYTES:
            raise ValueError("Choose a Markdown file smaller than 5 MB.")
        if voice not in VOICES:
            raise ValueError("Choose one of the available voices.")
        narration = markdown_text(source)
        chunks = split_chunks(narration, 1500)
        if not chunks:
            raise ValueError("This file has no readable text to narrate.")
        fingerprint = hashlib.sha256(json.dumps([narration, voice, DEFAULT_MODEL, SPEECH_STYLE]).encode()).hexdigest()
        with self.lock:
            for existing in self.jobs.values():
                if existing["fingerprint"] == fingerprint:
                    if existing["state"] in {"failed", "interrupted", "cancelled"}:
                        return self.resume(existing["id"])
                    return dict(existing)
            job_id = uuid.uuid4().hex
            folder = self.directory / job_id
            safe_stem = re.sub(r"[^\w -]", "", Path(filename).stem).strip()[:100] or "recording"
            folder.mkdir()
            (folder / "input.md").write_text(source, encoding="utf-8")
            (folder / "narration.txt").write_text(narration, encoding="utf-8")
            job = {
                "id": job_id, "filename": filename, "fingerprint": fingerprint,
                "voice": voice, "model": DEFAULT_MODEL, "state": "queued",
                "created_at": timestamp(), "completed": 0, "total": len(chunks),
                "words": len(narration.split()), "characters": len(narration),
                "key_switches": 0, "message": "Waiting to generate audio",
                "output_path": str((folder / f"{safe_stem}.wav").resolve()),
                "error": None,
            }
            self.jobs[job_id] = job
            self._save(job)
            self._submit(job_id)
            return dict(job)

    def _submit(self, job_id: str) -> None:
        cancel_event = threading.Event()
        self.cancel_events[job_id] = cancel_event
        self.worker.submit(self._run, job_id, cancel_event)

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs[job_id]
            if job["state"] in {"queued", "running", "cancelling"}:
                self.cancel_events[job_id].set()
                if job["state"] == "queued":
                    self._mark_cancelled(job)
                else:
                    job.update(state="cancelling", error=None,
                               message="Cancelling after the current request finishes. No further segments will start.")
                    self._save(job)
            return dict(job)

    def _mark_cancelled(self, job: dict) -> None:
        job.update(state="cancelled", error=None, finished_at=timestamp(),
                   message="Recording cancelled. Saved segments are kept for resuming.")
        self._save(job)

    def resume(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs[job_id]
            if job["state"] in {"queued", "running", "cancelling", "complete"}:
                return dict(job)
            job.update(state="queued", error=None, message="Resuming from saved audio segments")
            self._save(job)
            self._submit(job_id)
            return dict(job)

    def _run(self, job_id: str, cancel_event: threading.Event) -> None:
        def progress(event: dict) -> None:
            with self.lock:
                job = self.jobs[job_id]
                if job["state"] != "cancelling":
                    job["message"] = event["message"]
                if "completed" in event:
                    job["completed"] = event["completed"]
                if "total" in event:
                    job["total"] = event["total"]
                if "key" in event:
                    job["active_key"] = event["key"]
                if event["event"] == "key_switch":
                    job["key_switches"] += 1
                self._save(job)

        with self.lock:
            if self.cancel_events.get(job_id) is not cancel_event or cancel_event.is_set():
                return
            job = self.jobs[job_id]
            job.update(state="running", message="Preparing your narration")
            self._save(job)
        try:
            keys = self.key_loader()
            if not keys:
                raise ValueError("Add your Gemini API keys to the project's .env file, then click Resume.")
            convert(self.directory / job_id / "input.md", Path(job["output_path"]), keys,
                    voice=job["voice"], model=job["model"], generator=self.generator, progress=progress,
                    cancelled=cancel_event.is_set)
            with self.lock:
                if cancel_event.is_set():
                    self._mark_cancelled(job)
                else:
                    job.update(state="complete", message="Your recording is ready", finished_at=timestamp(), error=None)
                    self._save(job)
        except ConversionCancelled:
            with self.lock:
                self._mark_cancelled(job)
        except Exception as exc:
            with self.lock:
                if cancel_event.is_set():
                    self._mark_cancelled(job)
                else:
                    logging.exception("Audio job %s failed", job_id)
                    job.update(state="failed", error=str(exc), message="Generation paused. Saved segments are safe.")
                    self._save(job)

    def close(self) -> None:
        self.worker.shutdown(wait=True, cancel_futures=True)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store: JobStore):
        self.store = store
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logging.info(format, *args)

    def _headers(self, status: int, content_type: str, size: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'")

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _local_request(self) -> bool:
        host = self.headers.get("Host", "")
        if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
            self._json({"error": "Use the local Tovoice address."}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
            self._json({"error": "This request must come from the local Tovoice page."}, 403)
            return False
        return True

    def do_GET(self) -> None:
        if not self._local_request():
            return
        route = urlsplit(self.path).path
        try:
            if route == "/api/config":
                self._json({"key_count": len(self.server.store.key_loader()), "voices": VOICES, "model": DEFAULT_MODEL})
            elif route == "/api/jobs":
                self._json({"jobs": self.server.store.list()})
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}", route):
                self._json(self.server.store.get(route.split("/")[-1]))
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/(?:audio|transcript)", route):
                _, _, _, job_id, kind = route.split("/")
                job = self.server.store.get(job_id)
                if kind == "audio" and job["state"] != "complete":
                    self._json({"error": "The recording is still being generated."}, 409)
                    return
                path = Path(job["output_path"]) if kind == "audio" else self.server.store.directory / job_id / "narration.txt"
                self._file(path, download="download" in urlsplit(self.path).query, ranges=kind == "audio")
            else:
                assets = {"/": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
                if route not in assets:
                    self._json({"error": "Not found"}, 404)
                    return
                self._file(ROOT / "web" / assets[route])
        except (KeyError, FileNotFoundError):
            self._json({"error": "Recording not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _file(self, path: Path, download: bool = False, ranges: bool = False) -> None:
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        if ranges and self.headers.get("Range"):
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers["Range"])
            if not match or not any(match.groups()):
                self._json({"error": "Invalid audio range"}, 416)
                return
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]), size - 1) if match[2] else size - 1
            else:
                start = max(0, size - int(match[2]))
            if start > end or start >= size:
                self._json({"error": "Invalid audio range"}, 416)
                return
            status = 206
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._headers(status, content_type, end - start + 1)
        if ranges:
            self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            safe_name = re.sub(r"[^A-Za-z0-9._ -]", "", path.name)
            self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                data = stream.read(min(65536, remaining))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)

    def do_POST(self) -> None:
        if not self._local_request():
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._json({"error": "Send a JSON request."}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_UPLOAD_BYTES * 2:
                raise ValueError("Choose a Markdown file smaller than 5 MB.")
            data = json.loads(self.rfile.read(length))
            route = urlsplit(self.path).path
            if route == "/api/jobs":
                if not isinstance(data.get("filename"), str) or not isinstance(data.get("source"), str):
                    raise ValueError("A Markdown filename and text are required.")
                self._json(self.server.store.create(data["filename"], data["source"], data.get("voice", "Kore")), 201)
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/resume", route):
                self._json(self.server.store.resume(route.split("/")[3]))
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/cancel", route):
                self._json(self.server.store.cancel(route.split("/")[3]))
            else:
                self._json({"error": "Not found"}, 404)
        except KeyError:
            self._json({"error": "Recording not found"}, 404)
        except (ValueError, UnicodeError, AttributeError) as exc:
            self._json({"error": str(exc) if str(exc) else "Invalid upload"}, 400)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Tovoice UI")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the app in your browser")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = JobStore(ROOT / ".app-data" / "jobs")
    url = f"http://127.0.0.1:{args.port}"
    try:
        server = LocalServer(("127.0.0.1", args.port), store)
    except OSError:
        store.close()
        if args.open:
            webbrowser.open(url)
            return
        raise
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print(f"Tovoice is running at {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
