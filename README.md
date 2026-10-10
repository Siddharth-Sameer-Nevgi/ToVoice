# Tovoice

A local audio studio that reads a `.md` file, extracts readable prose, and uses Gemini text-to-speech to produce a `.wav` recording. It removes Markdown formatting, front matter, code blocks, HTML, and images; link labels and list text are kept. Inline identifiers and tables are turned into readable narration. Slashes, backslashes, arrows, and other notation are changed to words or removed before synthesis.

## Open the UI

Double-click **Start Tovoice.vbs** in this folder. It starts the backend in the background and opens the studio at **http://127.0.0.1:8765**. Your existing `.env` keys are loaded by the backend; they are never sent to the browser.

Choose a voice, then drop a Markdown file into the upload area or click **browse files**. Conversion starts automatically. You can watch segment progress, preview the cleaned narration, listen to the finished recording, and download the WAV. Files must be UTF-8 Markdown, up to 5 MB. Narration text is sent to Gemini for speech generation.

Uploads, narration text, recordings, and job history are stored in `.app-data/jobs/` on this computer. Each recording shows its full local path in the UI. If a job pauses, click **Resume recording**; successful segments are reused. Reuploading the same document with the same voice also resumes its job. Completed recordings stay in the library after reopening the app.

Click **Cancel recording** to stop a queued or active conversion. Queued jobs are cancelled immediately. An active job shows **Cancelling…** until its current Gemini request returns, then stops without starting another segment. Completed segments are kept, and **Resume recording** continues from them. Cancelling does not reverse an API request already sent to Gemini.

## Read along

After a recording finishes, the **Read along** panel highlights its corresponding text as the audio plays. **Markdown text** shows the words and identifiers from the original uploaded Markdown, with headings, lists, and table rows retained. **Spoken text** shows the cleaned narration. Click a word or passage to seek to it; **Follow audio** scrolls the text into view. Highlighting uses the audio player's time, so it follows pause, resume, seeking, and playback-speed changes.

The backend uses [Gemini's word timestamps](https://ai.google.dev/gemini-api/docs/transcribe) to measure the generated speech. This adds one transcription request per audio segment and sends the generated audio to Gemini. Requests use `store: false`. Word timings are cached locally beside each segment and share the same key fallback behavior. Existing recordings can get highlights without regenerating their audio.

Audio playback is available while timings are prepared. Until a segment's word timestamps are ready, its larger text section is highlighted using the measured WAV segment boundaries. If timing quota runs out, **Retry highlighting** resumes from cached timestamps. Recognition may misread technical terms or acronyms; changed phrases are mapped back to the original Markdown without replacing its text.

Closing the browser keeps background conversions running. After a computer restart, double-click the launcher again and resume any interrupted job from the library. Multiple uploads are processed one at a time.

The text is split into short segments. Each successful segment is saved beside the output in a hidden `.<output>.parts` folder. If an API key receives a quota/rate-limit response, the tool tries the next key for the **same segment**. The final WAV is assembled when all segments are ready.

## Setup

Use Python 3.10 or newer:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set `GEMINI_API_KEYS` to comma-separated keys. You can use `GEMINI_API_KEY` for a single key instead. The `.env` file is ignored by Git. **Never commit or share your API keys.**

The UI uses Python's built-in HTTP server and the same dependencies as the CLI. For development, start it with `python app.py --open`.

## Optional command-line use

```powershell
python md_to_audio.py .\example.md
python md_to_audio.py .\example.md --output .\recordings\example.wav --voice Kore
python md_to_audio.py .\example.md --preview-text
```

The default output is a `.wav` file beside the input. Use `--preview-text` to inspect exactly what will be sent to TTS without making an API call. `--max-chars` controls segment length (default 1500; minimum 100). `--model` defaults to `gemini-3.8-flash-lite-tts`. Rerunning after a narration-text change generates new segments and replaces the final WAV only when the new recording is complete.

Gemini rate limits are applied **per Google project, not per API key**. Multiple keys from one project share the same quota. For independent backup capacity, the keys need to belong to projects with available quota. A 429 response triggers rotation; 401 and 403 also rotate because a particular key may be unavailable. If every key fails, the tool exits and leaves successful segments for a later retry. Temporary server and network errors are retried briefly before failing. Other API errors stop the run so a bad request is visible.

## Check

```powershell
python -m unittest -v
node test_timeline.js
```

The tests use generated WAV fixtures and do not make Gemini API calls. A live recording needs your own keys and available TTS quota.
