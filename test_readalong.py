import io
import tempfile
import unittest
import wave
from pathlib import Path

import readalong
from md_to_audio import DEFAULT_MODEL, QuotaError, SynthesisError, convert, markdown_blocks, markdown_text, split_chunks


def audio_fixture(duration):
    result = io.BytesIO()
    with wave.open(result, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x01\x00" * int(24000 * duration))
    return result.getvalue()


def word_fixture(text, duration):
    words = text.split()
    step = (duration - 0.1) / len(words)
    return [{"text": word, "start": 0.05 + index * step, "end": 0.05 + (index + 0.8) * step}
            for index, word in enumerate(words)]


class ReadAlongTests(unittest.TestCase):
    def test_source_markdown_preserved_while_spoken_identifiers_are_mapped(self):
        source = "---\ntitle: Hidden\n---\n# Heading\n\nRead `lib/auth.ts` using `userId`."
        blocks = markdown_blocks(source)
        self.assertEqual(blocks[0]["line_start"], 4)
        self.assertEqual(blocks[0]["kind"], "heading")
        block = blocks[1]
        self.assertEqual(block["text"], "Read lib/auth.ts using userId.")
        self.assertEqual(block["narration"], "Read the auth file using user Id.")
        block.update(flat_start=0, flat_end=len(block["narration"]))
        block["word_map"] = readalong._source_word_map(block)
        cues = readalong.map_word_cues(block["narration"], word_fixture(block["narration"], 2), [block], 0, 0, 2)
        filename_range = cues[2]["ranges"][0]
        self.assertEqual(block["text"][filename_range["source_start"]:filename_range["source_end"]], "lib/auth.ts")
        identifier_range = cues[-1]["ranges"][0]
        self.assertEqual(block["text"][identifier_range["source_start"]:identifier_range["source_end"]], "userId.")

    def test_exact_segment_offsets_cached_word_timings_and_key_failover(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            source = "# Heading\n\n" + "Alpha beta gamma. " * 120
            input_path, output = Path(folder) / "story.md", Path(folder) / "story.wav"
            input_path.write_text(source, encoding="utf-8")
            chunks = split_chunks(markdown_text(source), 1500)
            self.assertEqual(len(chunks), 2)
            audio_calls = []

            def generate(*args):
                audio_calls.append(args[0])
                return audio_fixture(len(audio_calls))

            convert(input_path, output, ["good"], generator=generate, progress=lambda value: None)
            coarse = readalong.document(source, output, DEFAULT_MODEL, "Kore")
            self.assertEqual(coarse["precision"], "segment")
            self.assertEqual(coarse["cues"][1]["start"], 1)
            calls = []

            def exhausted(audio, key):
                calls.append(key)
                if key == "limited" or len(calls) == 3:
                    raise QuotaError("limit")
                return word_fixture(chunks[0], 1)

            with self.assertRaises(SynthesisError):
                readalong.prepare(source, output, DEFAULT_MODEL, "Kore", ["limited", "backup"], exhausted)
            self.assertEqual(calls, ["limited", "backup", "backup"])
            self.assertEqual(readalong.document(source, output, DEFAULT_MODEL, "Kore")["precision"], "mixed")
            retry_calls = []

            def retry(audio, key):
                retry_calls.append(key)
                return word_fixture(chunks[1], 2)

            readalong.prepare(source, output, DEFAULT_MODEL, "Kore", ["fresh"], retry)
            final = readalong.document(source, output, DEFAULT_MODEL, "Kore")
            self.assertEqual(final["precision"], "word")
            self.assertEqual(retry_calls, ["fresh"])
            self.assertEqual(len(audio_calls), 2)
            self.assertAlmostEqual(final["duration"], 3)
            self.assertAlmostEqual(final["cues"][len(chunks[0].split())]["start"], 1.05)
            self.assertTrue(all(0 <= cue["start"] < cue["end"] <= 3 for cue in final["cues"]))

    def test_parses_measured_offsets_and_rejects_unrelated_transcription(self):
        body = {"steps": [{"content": [{"annotations": [
            {"type": "word_info", "text": "Hello", "start_offset": "0.100s", "end_offset": "0.450s"},
            {"type": "word_info", "text": "world", "start_offset": "0.500s", "end_offset": "0.850s"},
        ]}]}]}
        words = readalong.parse_timestamps(body)
        self.assertEqual(words[1]["start"], 0.5)
        with self.assertRaises(SynthesisError):
            readalong.map_word_cues("An unrelated transcript.", words, [], 0, 0, 1)


if __name__ == "__main__":
    unittest.main()
