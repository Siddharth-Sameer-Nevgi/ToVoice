import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app import JobStore, LocalServer
from md_to_audio import QuotaError
from test_md_to_audio import wav_bytes


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.calls = []

        def generate(text, key, model, voice):
            self.calls.append((text, key))
            if key == "limited":
                raise QuotaError("quota reached")
            return wav_bytes(7)

        self.store = JobStore(Path(self.temp.name) / "jobs", lambda: ["limited", "backup"], generate, transcriber=None)
        self.server = LocalServer(("127.0.0.1", 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        headers = headers or {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
        return urllib.request.urlopen(req, timeout=15)

    def wait_for_job(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.store.get(job_id)
            if job["state"] not in {"queued", "running", "cancelling"}:
                return job
            time.sleep(0.01)
        self.fail("Audio worker did not finish")

    def test_upload_background_generation_backup_playback_and_deduplication(self):
        upload = {"filename": "story.md", "source": "# A story\n\nA clear lesson for PATIENT/DOCTOR roles.", "voice": "Kore"}
        with self.request("/api/jobs", upload) as response:
            job_id = json.load(response)["id"]
        job = self.wait_for_job(job_id)
        self.assertEqual(job["state"], "complete")
        self.assertEqual(job["completed"], job["total"])
        self.assertEqual(job["key_switches"], 1)
        self.assertEqual([call[1] for call in self.calls], ["limited", "backup"])
        with self.request(f"/api/jobs/{job_id}/transcript") as response:
            narration = response.read().decode()
        self.assertNotIn("/", narration)
        with self.request(f"/api/jobs/{job_id}/audio", headers={"Range": "bytes=0-3"}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"RIFF")
        with self.request("/api/jobs", upload) as response:
            self.assertEqual(json.load(response)["id"], job_id)
        self.assertEqual(len(self.calls), 2)

    def test_rejects_non_markdown_and_cross_origin_requests(self):
        for data, headers, code in [
            ({"filename": "bad.exe", "source": "Hello"}, {}, 400),
            ({"filename": "good.md", "source": "Hello"}, {"Origin": "https://example.com"}, 403),
        ]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/jobs", data, headers)
            self.assertEqual(error.exception.code, code)
        self.assertEqual(self.calls, [])

    def test_readalong_endpoint_automatically_prepares_and_preserves_markdown(self):
        self.store.key_loader = lambda: ["backup"]
        timing_calls = []

        def transcribe(audio, key):
            timing_calls.append(key)
            return [{"text": "Hello", "start": 0, "end": 0.0001},
                    {"text": "world", "start": 0.0001, "end": 0.0003}]

        self.store.transcriber = transcribe
        job = self.store.create("readalong.md", "Hello **world**.")
        self.wait_for_job(job["id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.store.get(job["id"]).get("sync_state") not in {"ready", "failed"}:
            time.sleep(0.01)
        with self.request(f"/api/jobs/{job['id']}/readalong") as response:
            data = json.load(response)
        self.assertEqual(data["sync_state"], "ready")
        self.assertEqual(data["precision"], "word")
        self.assertEqual(data["blocks"][0]["text"], "Hello world.")
        self.assertEqual(len(data["cues"]), 2)
        self.assertEqual(len(timing_calls), 1)
        # Restore missing timing artifacts without regenerating the recording.
        for path in (self.store.directory / job["id"]).rglob("*.words.json"):
            path.unlink()
        self.store.readalong_cache.clear()
        self.assertEqual(self.store.readalong_data(job["id"])["precision"], "segment")
        with self.request(f"/api/jobs/{job['id']}/sync", {}) as response:
            json.load(response)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.store.get(job["id"]).get("sync_state") not in {"ready", "failed"}:
            time.sleep(0.01)
        self.assertEqual(self.store.readalong_data(job["id"])["precision"], "word")
        self.assertEqual(len(timing_calls), 2)
        self.assertEqual(len(self.calls), 1)

    def test_job_resume_after_restart_reuses_saved_audio(self):
        job = self.store.create("restart.md", "A short restart example.")
        job = self.wait_for_job(job["id"])
        metadata = self.store.directory / job["id"] / "job.json"
        saved = json.loads(metadata.read_text())
        saved["state"] = "running"
        metadata.write_text(json.dumps(saved))
        self.store.close()

        def unexpected_generation(*args):
            raise AssertionError("Saved segment should have been reused")

        reopened = JobStore(self.store.directory, lambda: ["backup"], unexpected_generation, transcriber=None)
        self.store = reopened
        self.server.store = reopened
        self.assertEqual(reopened.get(job["id"])["state"], "interrupted")
        reopened.resume(job["id"])
        self.assertEqual(self.wait_for_job(job["id"])["state"], "complete")

    def test_cancel_running_job_preserves_segments_and_can_resume(self):
        in_request = threading.Event()
        release_request = threading.Event()
        calls = []

        def generate(text, key, model, voice):
            calls.append(text)
            if len(calls) == 2:
                in_request.set()
                if not release_request.wait(5):
                    raise TimeoutError("Test request did not finish")
            return wav_bytes(8)

        self.store.generator = generate
        self.store.key_loader = lambda: ["backup"]
        job = self.store.create("cancel.md", "A lesson in stopping and resuming. " * 150)
        self.assertTrue(in_request.wait(5))
        try:
            with self.request(f"/api/jobs/{job['id']}/cancel", {}) as response:
                self.assertEqual(json.load(response)["state"], "cancelling")
            self.assertEqual(self.store.resume(job["id"])["state"], "cancelling")
        finally:
            release_request.set()
        cancelled = self.wait_for_job(job["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIsNone(cancelled["error"])
        self.assertEqual(cancelled["completed"], 2)
        self.assertEqual(len(calls), 2)
        self.assertFalse(Path(cancelled["output_path"]).exists())
        with self.request(f"/api/jobs/{job['id']}/resume", {}) as response:
            json.load(response)
        completed = self.wait_for_job(job["id"])
        self.assertEqual(completed["state"], "complete")
        self.assertEqual(len(calls), completed["total"])

    def test_cancel_queued_job_then_resume_does_not_start_stale_attempt(self):
        in_request = threading.Event()
        release_request = threading.Event()
        calls = []

        def generate(text, key, model, voice):
            calls.append(text)
            if len(calls) == 1:
                in_request.set()
                if not release_request.wait(5):
                    raise TimeoutError("Test request did not finish")
            return wav_bytes(9)

        self.store.generator = generate
        self.store.key_loader = lambda: ["backup"]
        first = self.store.create("first.md", "First recording.")
        self.assertTrue(in_request.wait(5))
        try:
            queued = self.store.create("second.md", "Second recording.")
            with self.request(f"/api/jobs/{queued['id']}/cancel", {}) as response:
                self.assertEqual(json.load(response)["state"], "cancelled")
            metadata = self.store.directory / queued["id"] / "job.json"
            self.assertEqual(json.loads(metadata.read_text())["state"], "cancelled")
            self.assertEqual(len(calls), 1)
            self.store.resume(queued["id"])
        finally:
            release_request.set()
        self.assertEqual(self.wait_for_job(first["id"])["state"], "complete")
        self.assertEqual(self.wait_for_job(queued["id"])["state"], "complete")
        self.assertEqual(calls, ["First recording.", "Second recording."])


if __name__ == "__main__":
    unittest.main()
