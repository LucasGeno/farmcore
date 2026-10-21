(() => {
  const launcher = document.getElementById('assistant-launcher');
  const dialog = document.getElementById('assistant-dialog');
  if (!launcher || !dialog) return;
  const form = document.getElementById('assistant-form');
  const input = document.getElementById('assistant-question');
  const output = document.getElementById('assistant-answer');
  const status = document.getElementById('assistant-status');
  const submit = form.querySelector('button[type="submit"]');
  // Present only for the signed-in farmer: spray reports go through the chat app's draft → ✅.
  const messageUrl = dialog.dataset.messageUrl;
  const confirmUrl = dialog.dataset.confirmUrl;
  const greeting = status.textContent;
  let conversation = crypto.randomUUID();
  let lastActivity = Date.now();
  let generation = 0;
  let request = null;
  let recording = null;
  let mic = null;
  let busy = false;
  const TTL = 30 * 60 * 1000;
  const MAX_TURNS = 6;
  const say = text => { status.textContent = text; };
  function setBusy(value) {
    busy = value;
    submit.disabled = value;
    input.disabled = value;
    if (mic) mic.disabled = value;
    form.setAttribute('aria-busy', String(value));
  }
  function fresh(text) {
    generation++;
    request?.abort();
    if (recording?.state === 'recording') recording.stop();
    conversation = crypto.randomUUID();
    output.replaceChildren();
    input.value = '';
    lastActivity = Date.now();
    setBusy(false);
    say(text);
  }
  launcher.addEventListener('click', () => {
    if (Date.now() - lastActivity >= TTL) fresh('Previous conversation expired. ' + greeting);
    dialog.showModal(); input.focus(); setupVoice();
  });
  dialog.addEventListener('close', () => {
    if (recording?.state === 'recording') { generation++; recording.stop(); }
    launcher.focus();
  });
  document.getElementById('assistant-new').addEventListener('click', () => {
    fresh(greeting); input.focus();
  });
  function trim() {
    while (output.children.length > MAX_TURNS) output.firstElementChild.remove();
  }
  function entry(label, text, who) {
    const block = document.createElement('section');
    block.className = 'fc-assistant-turn';
    block.dataset.who = who;
    const name = document.createElement('strong');
    name.textContent = label;
    const paragraph = document.createElement('p');
    paragraph.textContent = text;
    block.append(name, paragraph);
    output.append(block);
    trim();
    return block;
  }
  function addLinks(block, links) {
    for (const [label, path] of links || []) {
      const linkUrl = new URL(path, location.origin);
      if (linkUrl.origin !== location.origin) continue;
      const link = document.createElement('a');
      link.href = linkUrl.href;
      link.textContent = label;
      link.className = 'block min-h-[44px] py-3 font-semibold underline';
      block.append(link);
    }
  }
  // Confirm / Cancel on a spray draft: one tap each, then both buttons go away.
  function addActions(block, actions) {
    if (!actions?.length || !confirmUrl) return;
    const row = document.createElement('div');
    row.className = 'fc-assistant-actions';
    for (const {label, action} of actions) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = label;
      button.className = action.startsWith('confirm:') ? 'fc-assistant-primary bg-brand-900 text-white' : 'fc-assistant-secondary';
      button.addEventListener('click', () => decide(row, action));
      row.append(button);
    }
    block.append(row);
    row.querySelector('button').focus();
  }
  function mutate(path, body) {
    const url = new URL(path, location.origin);
    if (url.origin !== location.origin) throw new Error('Foreign mutation URL');
    const headers = {'Accept': 'application/json'};
    const nonce = document.querySelector('meta[name="csrf-token"]')?.content;
    if (nonce) headers['X-CSRF-Token'] = nonce;
    return fetch(url, {method: 'POST', body, headers, credentials: 'same-origin',
      cache: 'no-store', signal: request.signal});
  }
  async function decide(row, action) {
    if (busy) return;
    const current = generation;
    row.querySelectorAll('button').forEach(b => { b.disabled = true; });
    setBusy(true);
    say(action.startsWith('confirm:') ? 'Saving…' : 'Cancelling…');
    request = new AbortController();
    try {
      const body = new FormData();
      body.append('action', action);
      const response = await mutate(confirmUrl, body);
      const answer = await response.json().catch(() => ({}));
      if (current !== generation) return;
      if (!response.ok) { row.querySelectorAll('button').forEach(b => { b.disabled = false; }); say(answer.text || 'Could not save. Please try again.'); return; }
      row.remove();
      const block = entry('Farm assistant', answer.text, 'assistant');
      addLinks(block, answer.links);
      lastActivity = Date.now();
      say(answer.kind === 'done' && answer.text.startsWith('✅') ? 'Saved to the register.' : greeting);
      block.scrollIntoView({block: 'nearest'});
    } catch (error) {
      if (current === generation && error.name !== 'AbortError') {
        row.querySelectorAll('button').forEach(b => { b.disabled = false; });
        say('Could not save. Please try again.');
      }
    } finally {
      if (current === generation) { setBusy(false); input.focus(); }
    }
  }
  async function send(question, viaVoice) {
    if (Date.now() - lastActivity >= TTL) fresh('Previous conversation expired.');
    const current = generation;
    const user = entry(viaVoice ? 'You (voice)' : 'You', question, 'user');
    setBusy(true);
    say('Reading your farm records…');
    request = new AbortController();
    let response;
    try {
      if (messageUrl) {
        const body = new FormData();
        body.append('q', question);
        body.append('conversation', conversation);
        response = await mutate(messageUrl, body);
      } else {
        const url = new URL(dialog.dataset.askUrl, location.origin);
        url.searchParams.set('q', question);
        url.searchParams.set('conversation', conversation);
        response = await fetch(url, {headers: {'Accept': 'application/json'}, cache: 'no-store', signal: request.signal});
      }
      const answer = await response.json();
      if (current !== generation) return;
      if (!response.ok) { say(answer.text || answer.detail || 'Could not read the records. Please try again.'); return; }
      if (answer.fresh_conversation) { output.replaceChildren(user); }
      const block = entry('Farm assistant', answer.text, 'assistant');
      addLinks(block, answer.links);
      addActions(block, answer.actions);
      trim();
      lastActivity = Date.now();
      input.value = '';
      say(answer.kind === 'draft' ? 'Check the draft, then Confirm.'
        : answer.kind === 'clarify' ? 'Answer the question, or say cancel.'
        : 'From your farm records. Ask a follow-up.');
      block.scrollIntoView({block: 'nearest'});
    } catch (error) {
      if (current === generation && error.name !== 'AbortError') say('Could not read the records. Please try again.');
    } finally {
      if (current === generation) {
        setBusy(false);
        if (!output.querySelector('.fc-assistant-actions')) input.focus();
      }
    }
  }
  form.addEventListener('submit', event => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question || busy || recording) return;
    send(question, false);
  });

  // Voice: hold the mic to talk, release to send (keyboard: press once to start, again to send).
  // Audio is transcribed on our server; the transcript is shown as your message and sent at once.
  // A misheard spray still stops at the draft's Confirm; a misheard question only reads records.
  const NO_SPEECH = 'Voice needs the speech service.';
  const MIN_MS = 500;
  const MIC = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>';
  let voiceChecked = false;
  async function setupVoice() {
    if (voiceChecked) return;
    voiceChecked = true;
    if (!window.MediaRecorder || !navigator.mediaDevices?.getUserMedia) return;
    let config;
    try {
      const response = await fetch('/assistant/voice', {cache: 'no-store'});
      config = await response.json();
    } catch (_) { return; }
    if (!config.voice) return;
    const limit = Math.min(30, config.max_seconds || 30) * 1000;
    mic = document.createElement('button');
    mic.type = 'button';
    mic.className = 'fc-assistant-mic';
    mic.setAttribute('aria-pressed', 'false');
    mic.setAttribute('aria-label', 'Hold to talk');
    mic.title = 'Hold to talk, release to send';
    mic.innerHTML = MIC + '<span class="fc-assistant-mic-label">Hold</span>';
    const label = mic.querySelector('span');
    submit.after(mic);
    mic.disabled = busy;
    let starting = false;
    let releaseEarly = false;
    let startedAt = 0;

    function release() {
      if (starting) { releaseEarly = true; return; }
      if (recording?.state === 'recording') recording.stop();
    }
    async function start() {
      if (busy || starting || recording) return;
      const current = generation;
      starting = true;
      releaseEarly = false;
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({audio: true});
        if (current !== generation || !dialog.open) { stream.getTracks().forEach(t => t.stop()); return; }
        recording = new MediaRecorder(stream);
      } catch (_) {
        stream?.getTracks().forEach(t => t.stop());
        recording = null;
        say('Microphone not available. Type your question instead.');
        return;
      } finally { starting = false; }
      const recorder = recording;
      const chunks = [];
      let timer;
      recorder.addEventListener('dataavailable', e => { if (e.data.size) chunks.push(e.data); });
      recorder.addEventListener('stop', async () => {
        clearTimeout(timer);
        stream.getTracks().forEach(t => t.stop());
        if (recording === recorder) recording = null;
        mic.setAttribute('aria-pressed', 'false');
        mic.classList.remove('is-recording');
        label.textContent = 'Hold';
        submit.disabled = busy;
        if (current !== generation || !dialog.open) return;
        if (Date.now() - startedAt < MIN_MS) { say('Hold the mic while you talk, then release.'); return; }
        setBusy(true);
        say('Transcribing…');
        const body = new FormData();
        const type = (chunks[0] && chunks[0].type) || 'audio/webm';
        body.append('audio', new Blob(chunks, {type}), 'question.webm');
        request = new AbortController();
        let text = '';
        try {
          const response = await fetch('/assistant/transcribe', {method: 'POST', body, signal: request.signal});
          const result = await response.json().catch(() => ({}));
          if (current !== generation) return;
          if (response.ok) {
            text = (result.text || '').trim();
            if (!text) say('Nothing heard. Try again or type.');
          } else if (response.status === 503) {
            say(NO_SPEECH);
          } else if (response.status === 413) {
            say('Recording too long. Keep it under 30 seconds.');
          } else {
            say('Could not transcribe. Type your question instead.');
          }
        } catch (error) {
          if (current === generation && error.name !== 'AbortError') say('Could not transcribe. Type your question instead.');
        } finally {
          if (current === generation) setBusy(false);
        }
        if (text && current === generation) send(text, true);
        else if (current === generation) input.focus();
      });
      try { recorder.start(); }
      catch (_) {
        recording = null; stream.getTracks().forEach(t => t.stop());
        say('Microphone not available. Type your question instead.'); return;
      }
      startedAt = Date.now();
      submit.disabled = true;
      mic.setAttribute('aria-pressed', 'true');
      mic.classList.add('is-recording');
      label.textContent = 'Recording';
      say('Recording… release to send.');
      timer = setTimeout(() => { if (recorder.state === 'recording') recorder.stop(); }, limit);
      if (releaseEarly) recorder.stop();
    }
    mic.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      mic.setPointerCapture?.(event.pointerId);
      start();
    });
    for (const type of ['pointerup', 'pointercancel']) mic.addEventListener(type, release);
    mic.addEventListener('contextmenu', event => event.preventDefault());
    // Keyboard and assistive tech send a click with no pointer: toggle instead of hold.
    mic.addEventListener('click', event => {
      if (event.detail !== 0) return;
      if (recording?.state === 'recording') { recording.stop(); return; }
      start();
    });
  }
})();
