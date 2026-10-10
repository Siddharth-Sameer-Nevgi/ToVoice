import io
import tempfile
import unittest
import wave
from pathlib import Path

from md_to_audio import ConversionCancelled, SynthesisError, convert, markdown_text, split_chunks


def wav_bytes(marker: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(bytes([marker, 0]) * 10)
    return buffer.getvalue()


class MarkdownAudioTests(unittest.TestCase):
    def test_extracts_prose_without_markup_or_code(self):
        source = (
            "---\ntitle: Hidden\n---\n# Hello **world**\n\n"
            "Read [this page](https://example.com) and `skip()` inline.\n\n"
            "![picture](image.png)\n\n```python\nsecret_code()\n```\n\n"
            "<div>hidden html</div>\n\n- First item\n- Second item\n"
        )
        self.assertEqual(
            markdown_text(source),
            "Hello world\n\nRead this page and inline.\n\nFirst item\n\nSecond item",
        )

    def test_symbols_paths_and_tables_become_readable(self):
        source = (
            "The PATIENT/DOCTOR\\ADMIN roles use `app/api/queue/route.ts`. "
            "Status moves BOOKED → CHECKED_IN.\n\n"
            "| Model | Note |\n|---|---|\n"
            "| `User` | `userId` identifies the account. |\n"
        )
        narration = markdown_text(source)
        self.assertIn("PATIENT or DOCTOR ADMIN", narration)
        self.assertIn("the route file", narration)
        self.assertIn("BOOKED then CHECKED IN", narration)
        self.assertIn("Model: User. Note: user Id identifies the account.", narration)
        self.assertNotIn("/", narration)
        self.assertNotIn("\\", narration)

    def test_chunks_respect_size_and_keep_words(self):
        text = "One sentence. Another sentence.\n\nA final paragraph."
        chunks = split_chunks(text, 100)
        self.assertEqual(" ".join(chunks), "One sentence. Another sentence. A final paragraph.")
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))

    def test_reuses_completed_segments_after_local_failure(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            source = Path(root) / "story.md"
            output = Path(root) / "story.wav"
            source.write_text("First part. " * 20 + "Second part. " * 20, encoding="utf-8")
            calls = []

            def first_run(chunk, voice):
                calls.append(chunk)
                if len(calls) == 1:
                    return wav_bytes(1)
                raise SynthesisError("local interruption")

            with self.assertRaises(SynthesisError):
                convert(source, output, max_chars=100, generator=first_run)
            self.assertEqual(len(calls), 2)
            self.assertFalse(output.exists())
            resumed_calls = []

            def second_run(chunk, voice):
                resumed_calls.append(chunk)
                return wav_bytes(2)

            convert(source, output, max_chars=100, generator=second_run)
            self.assertEqual(len(resumed_calls), len(split_chunks(markdown_text(source.read_text()), 100)) - 1)
            with wave.open(str(output), "rb") as reader:
                self.assertEqual(reader.getnframes(), 10 * (len(resumed_calls) + 1))

    def test_cancel_keeps_finished_segment_for_resume(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            source = Path(root) / "cancel.md"
            output = Path(root) / "cancel.wav"
            source.write_text("A short story.", encoding="utf-8")
            stopped = []

            def generate(chunk, voice):
                stopped.append(True)
                return wav_bytes(3)

            with self.assertRaises(ConversionCancelled):
                convert(source, output, generator=generate, cancelled=lambda: bool(stopped))
            self.assertFalse(output.exists())
            self.assertEqual(len(list(Path(root).rglob("00001.wav"))), 1)
            convert(source, output, generator=lambda *args: self.fail("Saved segment should be reused"))
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
