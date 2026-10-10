import io
import tempfile
import unittest
import wave
from pathlib import Path

import readalong
from local_audio import DEFAULT_VOICE
from md_to_audio import DEFAULT_MODEL, SynthesisError, convert, markdown_blocks, markdown_text, split_chunks


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

    def test_exact_segment_offsets_and_cached_local_alignment_resume(self):
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

            convert(input_path, output, generator=generate, progress=lambda value: None)
            coarse = readalong.document(source, output, DEFAULT_MODEL, DEFAULT_VOICE)
            self.assertEqual(coarse["precision"], "segment")
            self.assertEqual(coarse["cues"][1]["start"], 1)
            calls = []

            def interrupted(audio, text):
                calls.append(text)
                if len(calls) == 2:
                    raise SynthesisError("local alignment interruption")
                return word_fixture(chunks[0], 1)

            with self.assertRaises(SynthesisError):
                readalong.prepare(source, output, DEFAULT_MODEL, DEFAULT_VOICE, aligner=interrupted)
            self.assertEqual(calls, chunks)
            self.assertEqual(readalong.document(source, output, DEFAULT_MODEL, DEFAULT_VOICE)["precision"], "mixed")
            retry_calls = []

            def retry(audio, text):
                retry_calls.append(text)
                return word_fixture(chunks[1], 2)

            readalong.prepare(source, output, DEFAULT_MODEL, DEFAULT_VOICE, aligner=retry)
            final = readalong.document(source, output, DEFAULT_MODEL, DEFAULT_VOICE)
            self.assertEqual(final["precision"], "word")
            self.assertEqual(retry_calls, [chunks[1]])
            self.assertEqual(len(audio_calls), 2)
            self.assertAlmostEqual(final["duration"], 3)
            self.assertAlmostEqual(final["cues"][len(chunks[0].split())]["start"], 1.05)
            self.assertTrue(all(0 <= cue["start"] < cue["end"] <= 3 for cue in final["cues"]))

    def test_rejects_invalid_timing_and_unrelated_alignment(self):
        self.assertEqual(readalong.seconds("0.5s"), 0.5)
        with self.assertRaises(ValueError):
            readalong.seconds("nan")
        words = [{"text":"Hello", "start":0.1, "end":0.45}, {"text":"world", "start":0.5, "end":0.85}]
        with self.assertRaises(SynthesisError):
            readalong.map_word_cues("An unrelated transcript.", words, [], 0, 0, 1)


if __name__ == "__main__":
    unittest.main()
