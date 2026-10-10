const $ = (id) => document.getElementById(id);
let jobs = [];
let activeId = localStorage.getItem('tovoice-active');
let currentAudio = null;
let transcriptId = null;
let uploading = false;
let libraryStamp = '';
let cancelRequestId = null;
let readalongData = null;
let readalongId = null;
let readalongRevision = '';
let readerMode = localStorage.getItem('tovoice-text-mode') || 'source';
let readerNodes = new Map();
let highlighted = [];
let highlightedBlocks = [];
let currentCue = null;
let readerFrame = null;
let readalongLoadingId = null;

const statuses = { queued: 'In the queue', running: 'Creating audio', cancelling: 'Cancelling…', cancelled: 'Cancelled', complete: 'Ready to listen', failed: 'Paused', interrupted: 'Resume available' };
let voiceLabels = {'en_US-lessac-medium':'Lessac · Clear narrator','en_US-amy-medium':'Amy · Gentle narrator','en_US-ryan-medium':'Ryan · Steady narrator'};
const voiceName = voice => (voiceLabels[voice] || voice).split(' · ')[0];

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Something went wrong. Please try again.');
  return data;
}

function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { $('toast').hidden = true; }, 4500);
}

function connection(connected, text) {
  $('connection').replaceChildren();
  $('connection').append(document.createElement('span'), document.createTextNode(text));
  $('connection').classList.toggle('offline', !connected);
}

async function refresh() {
  try {
    const result = await api('/api/jobs');
    jobs = result.jobs;
    connection(true, 'Local Python processing');
    if (!jobs.some(job => job.id === activeId)) activeId = jobs[0]?.id || null;
    renderLibrary();
    renderActive();
  } catch (error) {
    connection(false, 'Reconnect to your studio');
    $('progress-label').textContent = 'Waiting for the local server. Reopen Tovoice if it stopped.';
  }
}

function renderLibrary() {
  $('library-count').textContent = jobs.length;
  $('recording-count').textContent = `${jobs.length} recording${jobs.length === 1 ? '' : 's'}`;
  if (!jobs.length) return;
  const stamp = JSON.stringify([activeId, jobs.map(job => [job.id, job.state, job.filename, job.voice, job.words])]);
  if (libraryStamp === stamp) return;
  libraryStamp = stamp;
  const list = $('recordings-list');
  list.replaceChildren();
  for (const job of jobs) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `recording-row${job.id === activeId ? ' selected' : ''}`;
    const icon = document.createElement('span');
    icon.className = 'file-symbol';
    icon.textContent = job.state === 'complete' ? '♪' : 'MD';
    const info = document.createElement('span');
    info.className = 'recording-info';
    const name = document.createElement('strong');
    name.textContent = job.filename;
    const details = document.createElement('small');
    const date = new Date(job.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    details.textContent = `${voiceName(job.voice)} voice · ${job.words.toLocaleString()} words · ${date}`;
    info.append(name, details);
    const badge = document.createElement('span');
    badge.className = `status-badge ${job.state}`;
    badge.textContent = statuses[job.state];
    const arrow = document.createElement('span');
    arrow.className = 'row-arrow';
    arrow.textContent = '↗';
    button.append(icon, info, badge, arrow);
    button.addEventListener('click', () => {
      selectJob(job.id);
      $('progress-title').scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
    list.append(button);
  }
}

function selectJob(id) {
  activeId = id;
  localStorage.setItem('tovoice-active', id);
  renderLibrary();
  renderActive();
}

function renderActive() {
  const job = jobs.find(item => item.id === activeId);
  $('empty-progress').hidden = Boolean(job);
  $('job-progress').hidden = !job;
  if (!job) return;
  $('job-filename').textContent = job.filename;
  $('job-details').textContent = `${job.words.toLocaleString()} words · ${voiceName(job.voice)} voice`;
  $('job-badge').className = `status-badge ${job.state}`;
  $('job-badge').textContent = statuses[job.state];
  const percent = job.state === 'complete' ? 100 : Math.floor(job.completed / job.total * 100);
  $('progress-percent').textContent = `${percent}%`;
  $('progress-bar').value = percent;
  $('progress-label').textContent = job.message;
  $('segments').textContent = `${job.completed} of ${job.total} segments saved`;
  $('engine-info').textContent = 'Processed on this computer';
  $('job-error').hidden = !job.error;
  $('job-error').textContent = job.error || '';
  $('resume-button').hidden = !['failed', 'interrupted', 'cancelled'].includes(job.state);
  $('resume-button').textContent = job.needs_local_restart ? 'Create locally ↗' : 'Resume recording ↗';
  $('cancel-button').hidden = !['queued', 'running', 'cancelling'].includes(job.state);
  $('cancel-button').disabled = job.state === 'cancelling' || cancelRequestId === job.id;
  $('cancel-button').textContent = job.state === 'cancelling' || cancelRequestId === job.id ? 'Cancelling…' : 'Cancel recording';
  $('audio-result').hidden = job.state !== 'complete';
  if (job.state === 'complete') {
    const url = `/api/jobs/${job.id}/audio`;
    if (currentAudio !== job.id) {
      $('audio-player').src = url;
      currentAudio = job.id;
    }
    $('download-audio').href = `${url}?download=1`;
    $('download-text').href = `/api/jobs/${job.id}/transcript?download=1`;
    $('output-path').textContent = job.output_path;
  } else if (currentAudio && currentAudio !== job.id) {
    $('audio-player').pause();
    currentAudio = null;
    $('audio-player').removeAttribute('src');
  }
  if (transcriptId !== job.id) {
    transcriptId = job.id;
    $('transcript-text').textContent = 'Loading narration…';
    fetch(`/api/jobs/${job.id}/transcript`).then(response => {
      if (!response.ok) throw new Error('Preview is unavailable.');
      return response.text();
    }).then(text => {
      if (transcriptId === job.id) $('transcript-text').textContent = text;
    }).catch(error => { $('transcript-text').textContent = error.message; });
  }
  updateReadAlong(job);
}

function updateReadAlong(job) {
  $('readalong-panel').hidden = false;
  $('readalong-filename').textContent = job.filename;
  $('reader-play').disabled = job.state !== 'complete';
  const revision = JSON.stringify([job.id, job.state, job.completed, job.sync_state, job.sync_completed]);
  if (readalongId !== job.id) {
    readalongId = job.id;
    readalongData = null;
    readalongRevision = '';
    clearHighlights();
    $('readalong-text').replaceChildren();
    $('sync-status').textContent = 'Loading your Markdown…';
    $('retry-sync').hidden = true;
  }
  if (revision !== readalongRevision && readalongLoadingId !== job.id) {
    const id = job.id;
    readalongLoadingId = id;
    api(`/api/jobs/${id}/readalong`).then(data => {
      if (activeId !== id) return;
      const firstLoad = !readalongData;
      readalongData = data;
      readalongRevision = revision;
      if (firstLoad) renderReaderText();
      updateSyncStatus(job, data);
      clearHighlights();
      updateHighlight();
    }).catch(error => {
      if (activeId === id) $('sync-status').textContent = error.message;
    }).finally(() => { if (readalongLoadingId === id) readalongLoadingId = null; });
  }
  updateReaderControls();
}

function updateSyncStatus(job, data) {
  $('retry-sync').hidden = !['failed', 'ready'].includes(data.sync_state) || data.precision === 'word' || job.state !== 'complete';
  if (job.state !== 'complete') {
    $('sync-status').textContent = job.needs_local_restart ? 'Create a local recording to listen with highlights. Your previous files remain saved.' : 'Your Markdown is ready to follow when the local recording finishes.';
  } else if (data.precision === 'word') {
    $('sync-status').textContent = 'Synchronized to spoken words. Your original Markdown text is preserved.';
  } else if (data.sync_state === 'failed') {
    $('sync-status').textContent = `${data.sync_error || 'Word highlighting is unavailable.'} Segment highlights remain available.`;
  } else if (data.sync_state === 'ready') {
    $('sync-status').textContent = 'Some word timings are missing. Retry highlighting to restore them. Larger sections still highlight with the audio.';
  } else {
    $('sync-status').textContent = `Preparing highlights locally (${data.aligned_segments} / ${data.total_segments}). Larger sections highlight until their word timings are ready.`;
  }
}

function renderReaderText() {
  clearHighlights();
  readerNodes = new Map();
  $('readalong-text').replaceChildren();
  $('source-mode').classList.toggle('selected', readerMode === 'source');
  $('narration-mode').classList.toggle('selected', readerMode === 'narration');
  $('source-mode').setAttribute('aria-pressed', readerMode === 'source');
  $('narration-mode').setAttribute('aria-pressed', readerMode === 'narration');
  if (!readalongData) return;
  const fragment = document.createDocumentFragment();
  for (const block of readalongData.blocks) {
    const section = document.createElement('div');
    section.className = `reader-block reader-${block.kind}`;
    section.dataset.block = block.id;
    section.tabIndex = 0;
    section.title = `Markdown line ${block.line_start}. Click to play this passage.`;
    const element = document.createElement(block.kind === 'heading' ? `h${Math.min(6, Math.max(2, block.level + 1))}` : 'p');
    const text = readerMode === 'source' ? block.text : block.narration;
    const words = [];
    let cursor = 0;
    for (const token of TovoiceTimeline.tokenize(text)) {
      element.append(document.createTextNode(token.before));
      const word = document.createElement('span');
      word.className = 'reader-word';
      word.textContent = token.text;
      word.dataset.start = token.start;
      word.dataset.end = token.end;
      element.append(word);
      words.push(word);
      cursor = token.offsetEnd;
    }
    element.append(document.createTextNode(text.slice(cursor)));
    section.append(element);
    fragment.append(section);
    readerNodes.set(block.id, {section, words});
  }
  $('readalong-text').append(fragment);
  currentCue = null;
  updateHighlight();
}

function clearHighlights() {
  for (const element of highlighted) element.classList.remove('spoken-word');
  for (const element of highlightedBlocks) element.classList.remove('spoken-block');
  highlighted = [];
  highlightedBlocks = [];
  currentCue = null;
}

function updateHighlight() {
  const audio = $('audio-player');
  const cue = readalongData && currentAudio === activeId ? TovoiceTimeline.cueAt(readalongData.cues, audio.currentTime) : null;
  updateReaderControls();
  if (cue === currentCue) return;
  clearHighlights();
  currentCue = cue;
  if (!cue) return;
  for (const range of cue.ranges) {
    const node = readerNodes.get(range.block);
    if (!node) continue;
    node.section.classList.add('spoken-block');
    highlightedBlocks.push(node.section);
    const begin = readerMode === 'source' ? range.source_start : range.narration_start;
    const end = readerMode === 'source' ? range.source_end : range.narration_end;
    for (const word of node.words) {
      if (TovoiceTimeline.overlaps(Number(word.dataset.start), Number(word.dataset.end), begin, end)) {
        word.classList.add('spoken-word');
        highlighted.push(word);
      }
    }
  }
  const target = highlighted[0] || highlightedBlocks[0];
  if (target && $('follow-audio').checked && !audio.paused) {
    const rect = target.getBoundingClientRect();
    const panel = $('readalong-text');
    const bounds = panel.getBoundingClientRect();
    if (rect.top < bounds.top + 20 || rect.bottom > bounds.bottom - 30) {
      panel.scrollBy({top:rect.top - bounds.top - panel.clientHeight * 0.35, behavior:'smooth'});
    }
  }
}

function clock(seconds) {
  if (!Number.isFinite(seconds)) return '0:00';
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
}

function updateReaderControls() {
  const audio = $('audio-player');
  $('reader-play').textContent = audio.paused ? 'Play audio' : 'Pause audio';
  $('reader-clock').textContent = `${clock(audio.currentTime)} / ${clock(audio.duration || readalongData?.duration || 0)}`;
}

function readerTick() {
  updateHighlight();
  readerFrame = $('audio-player').paused ? null : requestAnimationFrame(readerTick);
}

function jumpToText(event) {
  if (!readalongData || currentAudio !== activeId) return;
  const block = event.target.closest('.reader-block');
  if (!block) return;
  const word = event.target.closest('.reader-word');
  const cue = readalongData.cues.find(item => item.ranges.some(range => {
    if (range.block !== Number(block.dataset.block)) return false;
    const begin = readerMode === 'source' ? range.source_start : range.narration_start;
    const end = readerMode === 'source' ? range.source_end : range.narration_end;
    return !word || TovoiceTimeline.overlaps(Number(word.dataset.start), Number(word.dataset.end), begin, end);
  }));
  if (cue) {
    $('audio-player').currentTime = cue.start;
    updateHighlight();
    $('audio-player').play().catch(error => toast(error.message));
  }
}

for (const mode of ['source', 'narration']) $(`${mode}-mode`).addEventListener('click', () => {
  readerMode = mode;
  localStorage.setItem('tovoice-text-mode', mode);
  renderReaderText();
});
$('readalong-text').addEventListener('click', jumpToText);
$('readalong-text').addEventListener('keydown', event => {
  if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); jumpToText(event); }
});
$('reader-play').addEventListener('click', () => {
  const audio = $('audio-player');
  if (audio.paused) audio.play().catch(error => toast(error.message));
  else audio.pause();
});
$('audio-player').addEventListener('play', () => {
  if (readerFrame !== null) cancelAnimationFrame(readerFrame);
  readerTick();
});
for (const event of ['timeupdate', 'pause', 'seeking', 'seeked', 'ratechange', 'loadedmetadata', 'ended']) $('audio-player').addEventListener(event, updateHighlight);
$('retry-sync').addEventListener('click', async () => {
  const id = activeId;
  $('retry-sync').disabled = true;
  try {
    await api(`/api/jobs/${id}/sync`, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    readalongRevision = '';
    await refresh();
  } catch (error) { toast(error.message); }
  finally { $('retry-sync').disabled = false; }
});

async function upload(file) {
  if (!file || uploading) return;
  $('upload-error').hidden = true;
  if (!file.name.toLowerCase().endsWith('.md')) return showUploadError('Choose a Markdown (.md) file.');
  if (file.size > 5 * 1024 * 1024) return showUploadError('Choose a Markdown file smaller than 5 MB.');
  uploading = true;
  $('dropzone').disabled = true;
  try {
    const source = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
    const job = await api('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ filename: file.name, source, voice: $('voice').value }) });
    activeId = job.id;
    localStorage.setItem('tovoice-active', job.id);
    await refresh();
    toast(job.state === 'complete' ? 'This recording is already in your library.' : 'Document uploaded. Your audio is on its way.');
  } catch (error) {
    showUploadError(error instanceof TypeError && error.message.includes('encoding') ? 'Save your Markdown file as UTF-8, then upload it again.' : error.message);
  } finally {
    uploading = false;
    $('dropzone').disabled = false;
    $('file-input').value = '';
  }
}

function showUploadError(message) {
  $('upload-error').textContent = message;
  $('upload-error').hidden = false;
}

$('dropzone').addEventListener('click', () => $('file-input').click());
$('file-input').addEventListener('change', event => upload(event.target.files[0]));
for (const name of ['dragenter', 'dragover']) $('dropzone').addEventListener(name, event => { event.preventDefault(); $('dropzone').classList.add('dragging'); });
for (const name of ['dragleave', 'drop']) $('dropzone').addEventListener(name, event => { event.preventDefault(); $('dropzone').classList.remove('dragging'); });
$('dropzone').addEventListener('drop', event => upload(event.dataTransfer.files[0]));
window.addEventListener('dragover', event => event.preventDefault());
window.addEventListener('drop', event => event.preventDefault());
$('resume-button').addEventListener('click', async () => {
  $('resume-button').disabled = true;
  try {
    await api(`/api/jobs/${activeId}/resume`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    await refresh();
  } catch (error) { toast(error.message); }
  finally { $('resume-button').disabled = false; }
});

$('cancel-button').addEventListener('click', async () => {
  const jobId = activeId;
  cancelRequestId = jobId;
  renderActive();
  try {
    const updated = await api(`/api/jobs/${jobId}/cancel`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    jobs = jobs.map(job => job.id === jobId ? updated : job);
    renderLibrary();
    renderActive();
    toast(updated.state === 'cancelling' ? 'Stopping local generation. Saved segments will be kept.' : updated.state === 'cancelled' ? 'Recording cancelled. Saved segments are kept.' : 'This recording has already finished.');
  } catch (error) { toast(error.message); }
  finally { cancelRequestId = null; renderActive(); }
});

async function initialize() {
  try {
    const config = await api('/api/config');
    voiceLabels = config.voice_labels || voiceLabels;
    $('voice').replaceChildren();
    for (const voice of config.voices) {
      const option = document.createElement('option');
      option.value = voice;
      option.textContent = voiceLabels[voice] || voice;
      $('voice').append(option);
    }
    connection(true, 'Local Python processing');
  } catch (error) { connection(false, 'Local server unavailable'); }
  await refresh();
  setInterval(refresh, 1500);
}
initialize();
