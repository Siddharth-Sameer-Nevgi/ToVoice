# Tovoice

A local Python audio studio: upload a Markdown file, generate a natural voice recording, and follow the original text as the audio plays. Speech is generated with [Piper](https://github.com/OHF-Voice/piper1-gpl); word timings are computed locally with [stable-ts](https://github.com/jianfch/stable-ts) and Whisper. No API key is needed.

## Open the studio

Install Python 3.12, then double-click **Start Tovoice.vbs**. The launcher creates the project's virtual environment and installs its libraries if needed, starts the backend in the background, and opens **http://127.0.0.1:8765**. First-time library installation can take several minutes; its log is saved to `.app-data/setup.log`.

Choose **Lessac**, **Amy**, or **Ryan**, then drag in a UTF-8 `.md` file or click **browse files**. Conversion starts automatically. Watch segment progress, review narration text, listen to the recording, and download its WAV. Files may be up to 5 MB. The included voices and alignment model are for English narration.

The first use downloads public voice and alignment model files. After those files are cached, generation and highlighting run offline on the CPU. Documents and audio are never uploaded. Model files stay under `.app-data/models/` and are excluded from Git.

## Recordings and resume

Uploads, narration, recordings, job history, and audio segments are saved under `.app-data/jobs/`. Each completed recording shows its full local path. Closing the browser keeps conversions running. After a restart, reopen the studio and click **Resume recording** to continue from saved segments. Multiple uploads run one at a time.

Click **Cancel recording** to stop. Queued jobs cancel immediately; active jobs stop at a safe synthesis boundary. Completed segments remain saved and can be reused by Resume. Reuploading an identical document with the same voice also reuses its existing job.

Incomplete recordings made with a previous speech engine show **Create locally**. This starts a new local voice recording from the beginning, with a separate output filename, because voice models and audio formats cannot be mixed. The original files and segments remain on disk. Completed older recordings stay playable and can receive local alignment.

## Read along

Once the recording is ready, **Read along** highlights the corresponding text during playback. **Markdown text** shows words and identifiers from the uploaded document, retaining headings, lists, and table rows. **Spoken text** shows the cleaned narration. Click a word or passage to seek; **Follow audio** scrolls the text into view. Highlighting follows the audio clock, including seeks, pauses, and changes to playback speed.

Word timings are measured locally against the known narration and cached beside each segment. While alignment is running, larger sections highlight using measured WAV segment boundaries. If alignment stops, **Retry highlighting** continues from cached timings. Local alignment can take longer on slower CPUs, and technical terms may have imperfect word boundaries.

Markdown formatting, front matter, code blocks, HTML, and images are omitted from speech. Link labels and list prose are retained. Inline identifiers, tables, paths, slashes, and other symbols are cleaned into readable narration; the original Markdown text remains available in Read along.

## Development

The project supports Python 3.10–3.12 with pinned CPU PyTorch libraries. To set up explicitly:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe local_audio.py
.venv\Scripts\python.exe app.py --open
```

`local_audio.py` predownloads all three voices and the alignment model. WAV decoding and resampling happen in Python, so an external FFmpeg installation is not required for this app's recordings.

Optional CLI use:

```powershell
.venv\Scripts\python.exe md_to_audio.py .\example.md
.venv\Scripts\python.exe md_to_audio.py .\example.md --output .\recordings\example.wav --voice en_US-amy-medium
.venv\Scripts\python.exe md_to_audio.py .\example.md --preview-text
```

Default output is a WAV beside the input; default segment size is 1500 characters. `--preview-text` prints the cleaned narration without generating speech.

## Checks

```powershell
.venv\Scripts\python.exe -m unittest -v
node test_timeline.js
```

Unit tests use local audio fixtures and simulated engines. Models, user documents, recordings, environment files, and runtime data are ignored by Git.
