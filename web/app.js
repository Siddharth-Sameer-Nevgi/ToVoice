const $ = (id) => document.getElementById(id);
let jobs = [];
let activeId = localStorage.getItem('tovoice-active');
let currentAudio = null;
let transcriptId = null;
let uploading = false;
let keyCount = 0;
let libraryStamp = '';
let cancelRequestId = null;

const statuses = { queued: 'In the queue', running: 'Creating audio', cancelling: 'Cancelling…', cancelled: 'Cancelled', complete: 'Ready to listen', failed: 'Paused', interrupted: 'Resume available' };
const voiceLabels = { Kore: 'Balanced', Puck: 'Upbeat', Aoede: 'Breezy', Charon: 'Informative', Fenrir: 'Excitable', Leda: 'Youthful', Orus: 'Firm', Zephyr: 'Bright' };

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
    connection(keyCount > 0, keyCount ? `${keyCount} Gemini keys configured` : 'Add Gemini keys in .env');
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
    details.textContent = `${job.voice} voice · ${job.words.toLocaleString()} words · ${date}`;
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
  $('job-details').textContent = `${job.words.toLocaleString()} words · ${job.voice} voice`;
  $('job-badge').className = `status-badge ${job.state}`;
  $('job-badge').textContent = statuses[job.state];
  const percent = job.state === 'complete' ? 100 : Math.floor(job.completed / job.total * 100);
  $('progress-percent').textContent = `${percent}%`;
  $('progress-bar').value = percent;
  $('progress-label').textContent = job.message;
  $('segments').textContent = `${job.completed} of ${job.total} segments saved`;
  $('key-info').textContent = job.key_switches ? `${job.key_switches} backup switch${job.key_switches === 1 ? '' : 'es'}` : 'Automatic backup keys';
  $('job-error').hidden = !job.error;
  $('job-error').textContent = job.error || '';
  $('resume-button').hidden = !['failed', 'interrupted', 'cancelled'].includes(job.state);
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
}

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
    toast(updated.state === 'cancelling' ? 'Cancellation requested. The current request may finish; saved segments will be kept.' : updated.state === 'cancelled' ? 'Recording cancelled. Saved segments are kept.' : 'This recording has already finished.');
  } catch (error) { toast(error.message); }
  finally { cancelRequestId = null; renderActive(); }
});

async function initialize() {
  try {
    const config = await api('/api/config');
    $('voice').replaceChildren();
    for (const voice of config.voices) {
      const option = document.createElement('option');
      option.value = voice;
      option.textContent = `${voice} · ${voiceLabels[voice] || 'Narrator'}`;
      $('voice').append(option);
    }
    keyCount = config.key_count;
    connection(keyCount > 0, keyCount ? `${keyCount} Gemini keys configured` : 'Add Gemini keys in .env');
  } catch (error) { connection(false, 'Local server unavailable'); }
  await refresh();
  setInterval(refresh, 1500);
}
initialize();
