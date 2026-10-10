"""Local Tovoice web application. All uploads and recordings stay on disk."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import re
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import readalong
import local_audio

from md_to_audio import ConversionCancelled, DEFAULT_MODEL, SPEECH_STYLE, _atomic_write, convert, markdown_text, split_chunks, synthesize


ROOT = Path(__file__).resolve().parent
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
VOICES = local_audio.VOICES


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, directory: Path, generator=synthesize, aligner=readalong.align_words):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.generator = generator
        self.aligner = aligner
        self.lock = threading.RLock()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tovoice")
        self.sync_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="readalong")
        self.readalong_cache: dict[str, tuple] = {}
        self.jobs: dict[str, dict] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        for path in directory.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job["state"] in {"running", "queued"}:
                    job.update(state="interrupted", message="Server restarted. Resume to continue saved segments.")
                elif job["state"] == "cancelling":
                    job.update(state="cancelled", error=None, message="Recording cancelled. Saved segments are kept for resuming.")
                if job.get("sync_state") in {"queued", "running"}:
                    job["sync_state"] = "waiting"
                elif job.get("sync_state") == "ready" and job.get("alignment_engine") != local_audio.ALIGNMENT_ENGINE:
                    job.update(sync_state="waiting", sync_completed=0, sync_error=None)
                if job["model"] != DEFAULT_MODEL and job["state"] != "complete":
                    job.update(needs_local_restart=True, error=None,
                               message="This recording used a previous voice. Create locally to restart with an offline voice; saved files are kept.")
                else:
                    job.setdefault("needs_local_restart", False)
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

    def create(self, filename: str, source: str, voice: str = local_audio.DEFAULT_VOICE) -> dict:
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
                "message": "Waiting to generate audio locally", "needs_local_restart": False,
                "output_path": str((folder / f"{safe_stem}.wav").resolve()),
                "error": None,
                "sync_state": "waiting", "sync_completed": 0, "sync_error": None,
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
                               message="Cancelling local generation. Completed segments are kept.")
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
            if job.get("needs_local_restart"):
                previous_output = Path(job["output_path"])
                narration = (self.directory / job_id / "narration.txt").read_text(encoding="utf-8")
                job.update(model=DEFAULT_MODEL, voice=local_audio.DEFAULT_VOICE, completed=0,
                           previous_output_path=str(previous_output),
                           output_path=str(previous_output.with_name(previous_output.stem + "-local.wav")),
                           needs_local_restart=False, sync_state="waiting", sync_completed=0, sync_error=None,
                           fingerprint=hashlib.sha256(json.dumps([narration, local_audio.DEFAULT_VOICE, DEFAULT_MODEL, SPEECH_STYLE]).encode()).hexdigest())
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
                self._save(job)

        with self.lock:
            if self.cancel_events.get(job_id) is not cancel_event or cancel_event.is_set():
                return
            job = self.jobs[job_id]
            job.update(state="running", message="Preparing your narration")
            self._save(job)
        try:
            convert(self.directory / job_id / "input.md", Path(job["output_path"]),
                    voice=job["voice"], model=job["model"], generator=self.generator, progress=progress,
                    cancelled=cancel_event.is_set)
            with self.lock:
                if cancel_event.is_set():
                    self._mark_cancelled(job)
                else:
                    job.update(state="complete", message="Your recording is ready", finished_at=timestamp(), error=None)
                    self._save(job)
            if job["state"] == "complete":
                self.ensure_sync(job_id)
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

    def ensure_sync(self, job_id: str, retry: bool = False) -> dict:
        with self.lock:
            job = self.jobs[job_id]
            state = job.get("sync_state", "waiting")
            if self.aligner and job["state"] == "complete" and state not in {"queued", "running"} and (state not in {"failed", "ready"} or retry):
                job.update(sync_state="queued", sync_error=None, sync_message="Preparing synchronized highlights")
                self.readalong_cache.pop(job_id, None)
                self._save(job)
                self.sync_worker.submit(self._run_sync, job_id)
            return dict(job)

    def _run_sync(self, job_id: str) -> None:
        def status(message):
            with self.lock:
                job["sync_message"] = message
                self._save(job)

        def progress(number):
            with self.lock:
                job.update(sync_completed=number, sync_message=f"Preparing highlights: {number} of {job['total']} segments")
                self.readalong_cache.pop(job_id, None)
                self._save(job)

        with self.lock:
            job = self.jobs[job_id]
            job.update(sync_state="running", sync_message="Matching spoken words to your Markdown")
            self._save(job)
        try:
            source = (self.directory / job_id / "input.md").read_text(encoding="utf-8")
            readalong.prepare(source, Path(job["output_path"]), job["model"], job["voice"], self.aligner, progress, status)
            with self.lock:
                job.update(sync_state="ready", sync_error=None, sync_message="Synchronized highlights ready", alignment_engine=local_audio.ALIGNMENT_ENGINE)
                self.readalong_cache.pop(job_id, None)
                self._save(job)
        except Exception as exc:
            logging.warning("Read-along preparation for %s failed: %s", job_id, exc)
            with self.lock:
                job.update(sync_state="failed", sync_error=str(exc), sync_message="Audio is ready. Highlight preparation paused.")
                self._save(job)

    def readalong_data(self, job_id: str) -> dict:
        job = self.ensure_sync(job_id)
        revision = (job["state"], job["completed"], job.get("sync_completed", 0), job.get("sync_state"))
        with self.lock:
            cached = self.readalong_cache.get(job_id)
        if not cached or cached[0] != revision:
            source = (self.directory / job_id / "input.md").read_text(encoding="utf-8")
            data = readalong.document(source, Path(job["output_path"]), job["model"], job["voice"])
            with self.lock:
                self.readalong_cache[job_id] = (revision, data)
        else:
            data = cached[1]
        return {**data, "sync_state": job.get("sync_state", "waiting"), "sync_error": job.get("sync_error"),
                "sync_message": job.get("sync_message", "Highlights will be prepared when the audio is ready.")}

    def close(self) -> None:
        self.worker.shutdown(wait=True, cancel_futures=True)
        self.sync_worker.shutdown(wait=True, cancel_futures=True)


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
                self._json({"engine": "local", "voices": VOICES, "voice_labels": local_audio.VOICE_LABELS,
                            "model": DEFAULT_MODEL, "ready": local_audio.models_ready()})
            elif route == "/api/jobs":
                self._json({"jobs": self.server.store.list()})
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}", route):
                self._json(self.server.store.get(route.split("/")[-1]))
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/readalong", route):
                self._json(self.server.store.readalong_data(route.split("/")[3]))
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/(?:audio|transcript)", route):
                _, _, _, job_id, kind = route.split("/")
                job = self.server.store.get(job_id)
                if kind == "audio" and job["state"] != "complete":
                    self._json({"error": "The recording is still being generated."}, 409)
                    return
                path = Path(job["output_path"]) if kind == "audio" else self.server.store.directory / job_id / "narration.txt"
                self._file(path, download="download" in urlsplit(self.path).query, ranges=kind == "audio")
            else:
                assets = {"/": "index.html", "/app.js": "app.js", "/timeline.js": "timeline.js", "/styles.css": "styles.css"}
                if route not in assets:
                    self._json({"error": "Not found"}, 404)
                    return
                self._file(ROOT / "web" / assets[route])
        except (KeyError, FileNotFoundError):
            self._json({"error": "Recording not found"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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
                self._json(self.server.store.create(data["filename"], data["source"], data.get("voice", local_audio.DEFAULT_VOICE)), 201)
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/resume", route):
                self._json(self.server.store.resume(route.split("/")[3]))
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/cancel", route):
                self._json(self.server.store.cancel(route.split("/")[3]))
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/sync", route):
                self._json(self.server.store.ensure_sync(route.split("/")[3], retry=True))
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
