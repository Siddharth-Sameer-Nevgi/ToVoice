import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_audio
from audio_errors import ConversionCancelled, SynthesisError


class ModelCacheTests(unittest.TestCase):
    def test_public_model_downloads_are_cached_and_send_no_document(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            calls = []

            def download(request, timeout):
                calls.append(request)
                self.assertIsNone(request.data)
                self.assertEqual(request.get_method(), "GET")
                data = b'{}' if request.full_url.endswith('.json') else b'test model bytes'
                response = io.BytesIO(data)
                response.headers = {"Content-Length": str(len(data))}
                return response

            with patch.object(local_audio, "MODEL_DIR", Path(folder)), patch("local_audio._open_download", download):
                path = local_audio.ensure_voice(local_audio.DEFAULT_VOICE)
                self.assertTrue(path.exists())
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(request.full_url.startswith('https://huggingface.co/rhasspy/piper-voices/resolve/main/') for request in calls))
                with patch("local_audio._open_download", side_effect=AssertionError("Offline cache must not use networking")):
                    self.assertEqual(local_audio.ensure_voice(local_audio.DEFAULT_VOICE), path)

    def test_cancelled_download_removes_partial_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            response = io.BytesIO(b'model data')
            response.headers = {}
            path = Path(folder) / 'model.onnx'
            with patch("local_audio._open_download", return_value=response):
                with self.assertRaises(ConversionCancelled):
                    local_audio._download('https://huggingface.co/model', path, cancelled=lambda: True)
            self.assertFalse(path.exists())
            self.assertFalse(path.with_suffix('.onnx.partial').exists())

    def test_unknown_voice_is_rejected_before_file_or_network_access(self):
        with patch("local_audio._open_download", side_effect=AssertionError("No network allowed")):
            with self.assertRaises(SynthesisError):
                local_audio.voice_paths('../../outside-model')

    def test_incomplete_transfer_resumes_from_bytes_already_saved(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            requested = []

            def download(request, timeout):
                requested.append(request.get_header('Range'))
                data, span = (b'012', 'bytes 0-5/10') if len(requested) == 1 else (b'3456789', 'bytes 3-9/10')
                response = io.BytesIO(data)
                response.headers = {'Content-Range': span}
                return response

            path = Path(folder) / 'model.onnx'
            with patch('local_audio._open_download', download), patch('local_audio.time.sleep'):
                local_audio._download('https://huggingface.co/model', path)
            self.assertEqual(path.read_bytes(), b'0123456789')
            self.assertTrue(requested[1].startswith('bytes=3-'))


if __name__ == '__main__':
    unittest.main()
