/* AYAN: a small, dependency-free interface for the local assistant. */
(() => {
  'use strict';
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const icons = (name) => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.append(use);
    return svg;
  };
  const stored = (key, fallback) => {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  };
  const remember = (key, value) => {
    try { localStorage.setItem(key, String(value)); } catch { /* Storage may be unavailable. */ }
  };
  const clampVolume = (value) => Math.max(0, Math.min(100, Number.isFinite(Number(value)) ? Number(value) : 60));
  const state = {
    view: 'home', volume: clampVolume(stored('ayan-volume', '60')),
    speech: stored('ayan-speech', 'true') === 'true',
    language: stored('ayan-language', 'kk-KZ'), health: null,
    recognition: null, listening: false, nativeListening: false,
    player: null, playerReady: false, currentTrack: null, playing: false,
    youtubePromise: null, commandBusy: false, browserBusy: false,
    entries: [], reminders: [], notifiedReminders: new Set(),
    history: [], lastEngine: null, settingsPromise: null,
    settings: { llm_enabled: true, llm_provider: 'ollama', ollama_model: 'qwen2.5:3b', llm_model: 'gpt-4.1-mini', llm_configured: false, tts_provider: 'edge', tts_voice: 'kk-KZ-DauletNeural', elevenlabs_configured: false, elevenlabs_voice_id: '', elevenlabs_model: 'eleven_v3', speech_style: 'serious', speech_rate: -8, speech_pitch: -8, wake_enabled: true, wake_language: 'kk-KZ' },
    speechGeneration: 0, speechAbort: null, speechAudio: null, speechUrl: null,
    lastSpeechText: '', lastSpeechLanguage: 'kk-KZ',
    speechBusy: false, wakeStatus: null, wakeCursor: 0, wakeSeen: new Set(), wakePrimed: false,
    wakePollTimer: null, wakePollRunning: false, wakeInstalling: false, wakeChanging: false,
    wakeActivityPromise: null, wakeActivityLast: null, wakeActivityForce: false, closing: false,
    playlist: [], playlistQueue: [], playlistQueueIndex: -1, playlistQueueActive: false,
    listenSession: null, musicCandidates: [], failedMusicIds: new Set(), musicRecoveryBusy: false,
    ollamaStatus: null, ollamaChecking: false, ollamaCheckId: 0,
  };
  const viewNames = { home: 'Басты бет', music: 'Музыка', anime: 'Аниме', browser: 'Браузер', diary: 'Күнделік', reminders: 'Еске салғыштар', settings: 'Баптаулар' };
  const mobileMedia = window.matchMedia('(max-width: 650px)');
  const months = ['қаңтар', 'ақпан', 'наурыз', 'сәуір', 'мамыр', 'маусым', 'шілде', 'тамыз', 'қыркүйек', 'қазан', 'қараша', 'желтоқсан'];
  function syncSidebar() { $('#sidebar').inert = mobileMedia.matches && !$('#sidebar').classList.contains('open'); }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const { timeout = 45000, ...fetchOptions } = options;
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, {
        ...fetchOptions, signal: controller.signal,
        headers: { 'Content-Type': 'application/json', ...(fetchOptions.headers || {}) },
      });
      let body;
      try { body = await response.json(); } catch { body = {}; }
      if (!response.ok) {
        const detail = body.detail;
        throw new Error(typeof detail === 'string' ? detail : `Сұрау орындалмады (${response.status}).`);
      }
      return body;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('Сұрау тым ұзақ орындалуда. Біраздан кейін қайталап көріңіз.');
      if (error instanceof TypeError) throw new Error('Көмекшіге қосылу мүмкін болмады. Бағдарлама іске қосылғанын тексеріңіз.');
      throw error;
    } finally { clearTimeout(timer); }
  }
  const post = (path, body, options = {}) => api(path, { method: 'POST', body: JSON.stringify(body), ...options });

  function toast(message, error = false, persistent = false) {
    const node = document.createElement('div');
    node.className = `toast${error ? ' error' : ''}`;
    node.append(icons(error ? 'close' : 'check'));
    const text = document.createElement('span');
    text.textContent = message;
    const close = document.createElement('button');
    close.textContent = '×';
    close.setAttribute('aria-label', 'Хабарламаны жабу');
    close.addEventListener('click', () => node.remove());
    node.append(text, close);
    $('#toast-stack').append(node);
    while ($('#toast-stack').children.length > 4) $('#toast-stack').firstElementChild.remove();
    if (!persistent) setTimeout(() => node.remove(), error ? 9000 : 6500);
  }
  function speechFeedback(description, { retry = false, stop = false, error = false } = {}) {
    $('#speech-feedback').hidden = !description;
    $('#speech-feedback').classList.toggle('error', error);
    $('#speech-status').textContent = description;
    $('#speech-retry').hidden = !retry;
    $('#speech-stop').hidden = !stop;
  }
  function releaseSpeechAudio() {
    if (state.speechAudio) {
      state.speechAudio.onended = null;
      state.speechAudio.onerror = null;
      state.speechAudio.pause();
      state.speechAudio.src = '';
      state.speechAudio = null;
    }
    if (state.speechUrl) { URL.revokeObjectURL(state.speechUrl); state.speechUrl = null; }
  }
  function stopSpeech(description = '') {
    state.speechGeneration += 1;
    state.speechAbort?.abort();
    state.speechAbort = null;
    releaseSpeechAudio();
    if ('speechSynthesis' in window) speechSynthesis.cancel();
    setSpeechBusy(false);
    speechFeedback(description);
  }
  async function playSpeechAudio(audio, generation) {
    try {
      setSpeechBusy(true);
      if (!(await syncWakeActivity())) throw new Error('Шақыруды қауіпсіз үзу мүмкін болмады.');
      if (generation !== state.speechGeneration || !state.speech) return;
      await audio.play();
      if (generation === state.speechGeneration) speechFeedback('Жауап дауысталып жатыр.', { stop: true });
    } catch (error) {
      if (generation !== state.speechGeneration) return;
      setSpeechBusy(false);
      if (error.name === 'NotAllowedError') speechFeedback('Жауап дайын. Тыңдау үшін батырманы басыңыз.', { retry: true });
      else {
        releaseSpeechAudio();
        speechFeedback('Дыбысты ойнату мүмкін болмады. Жауапты мәтіннен оқыңыз.', { retry: true, error: true });
      }
    }
  }
  async function speak(content, language = state.language) {
    state.lastSpeechText = String(content || '');
    state.lastSpeechLanguage = language;
    if (!state.speech || !state.lastSpeechText) return;
    stopSpeech();
    const generation = state.speechGeneration;
    const text = Array.from(state.lastSpeechText).slice(0, 2000).join('');
    if (state.settingsPromise) await state.settingsPromise.catch(() => {});
    if (generation !== state.speechGeneration || !state.speech) return;
    setSpeechBusy(true);
    if (!(await syncWakeActivity())) {
      if (generation !== state.speechGeneration) return;
      setSpeechBusy(false);
      speechFeedback('Шақыруды үзу мүмкін болмады. Жауапты мәтіннен оқыңыз.', { retry: true, error: true });
      return;
    }
    if (generation !== state.speechGeneration || !state.speech) return;
    if (state.settings.tts_provider === 'system') {
      const matchingVoice = 'speechSynthesis' in window && speechSynthesis.getVoices().find((voice) =>
        voice.lang.toLowerCase().replace('_', '-').split('-')[0] === language.toLowerCase().split('-')[0]);
      if (!matchingVoice) {
        setSpeechBusy(false);
        speechFeedback('Компьютерде осы тілдің дауысы жоқ. Баптаулардан Edge дауысын таңдаңыз немесе жауапты мәтіннен оқыңыз.', { retry: true, error: true });
        return;
      }
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.voice = matchingVoice; utterance.lang = matchingVoice.lang; utterance.volume = state.volume / 100;
      utterance.onstart = () => { if (generation === state.speechGeneration) speechFeedback('Жауап дауысталып жатыр.', { stop: true }); };
      utterance.onend = () => { if (generation === state.speechGeneration) { setSpeechBusy(false); speechFeedback('Жауап дауысталды.'); } };
      utterance.onerror = () => { if (generation === state.speechGeneration) { setSpeechBusy(false); speechFeedback('Компьютер дауысы іске қосылмады. Жауап мәтінде сақталды.', { retry: true, error: true }); } };
      speechSynthesis.speak(utterance);
      return;
    }
    const controller = new AbortController();
    state.speechAbort = controller;
    const timer = setTimeout(() => controller.abort(), 45000);
    speechFeedback('Таңдалған дауыс дайындалуда…', { stop: true });
    try {
      const response = await fetch('/api/speech', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, language }), signal: controller.signal,
      });
      if (!response.ok) {
        let detail;
        try { detail = (await response.json()).detail; } catch { /* A non-JSON service failure is still reported. */ }
        throw new Error(typeof detail === 'string' ? detail : `Дауыс қызметі жауап бермеді (${response.status}).`);
      }
      if (!String(response.headers.get('content-type') || '').startsWith('audio/')) throw new Error('Дауыс қызметі дыбыс файлын қайтармады.');
      const blob = await response.blob();
      if (generation !== state.speechGeneration || !state.speech) return;
      if (!blob.size) throw new Error('Дауыс қызметі бос дыбыс файлын қайтарды.');
      state.speechUrl = URL.createObjectURL(blob);
      const audio = new Audio(state.speechUrl);
      state.speechAudio = audio; audio.volume = state.volume / 100;
      audio.onended = () => {
        if (generation !== state.speechGeneration) return;
        releaseSpeechAudio(); setSpeechBusy(false); speechFeedback('Жауап дауысталды.');
      };
      audio.onerror = () => {
        if (generation !== state.speechGeneration) return;
        releaseSpeechAudio(); setSpeechBusy(false); speechFeedback('Дыбыс файлы ойнатылмады. Жауап мәтінде сақталды.', { retry: true, error: true });
      };
      await playSpeechAudio(audio, generation);
    } catch (error) {
      if (generation !== state.speechGeneration || !state.speech) return;
      setSpeechBusy(false);
      const detail = error.name === 'AbortError' ? 'Дауыс қызметінің жауабы тым ұзақ күттірді.' : error.message;
      speechFeedback(`${detail} Жауапты мәтіннен оқи аласыз.`, { retry: true, error: true });
    } finally {
      clearTimeout(timer);
      if (state.speechAbort === controller) state.speechAbort = null;
    }
  }
  function message(content, role = 'assistant', error = false, shouldSpeak = true) {
    const node = document.createElement('div');
    node.className = `chat-message ${role}${error ? ' error' : ''}`;
    const avatar = document.createElement('span');
    avatar.className = 'chat-avatar';
    avatar.textContent = role === 'assistant' ? 'A' : 'С';
    avatar.setAttribute('aria-hidden', 'true');
    const paragraph = document.createElement('p');
    paragraph.textContent = content;
    node.append(avatar, paragraph);
    const conversation = $('#conversation');
    conversation.append(node);
    while (conversation.children.length > 24) conversation.firstElementChild.remove();
    conversation.classList.add('has-messages');
    conversation.scrollTop = conversation.scrollHeight;
    state.history.push({ role, content: String(content) });
    if (state.history.length > 8) state.history = state.history.slice(-8);
    if (role === 'assistant' && !error && shouldSpeak) speak(content);
  }
  function showView(view, updateHash = true) {
    if (!Object.hasOwn(viewNames, view)) view = 'home';
    state.view = view;
    $('#main-content').dataset.view = view;
    $('#page-name').textContent = viewNames[view];
    $$('[data-page]').forEach((element) => { element.hidden = !element.dataset.page.split(' ').includes(view); });
    $$('.nav-item').forEach((button) => {
      const selected = button.dataset.view === view;
      button.classList.toggle('active', selected);
      if (selected) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    $('#sidebar').classList.remove('open');
    $('#menu-toggle').setAttribute('aria-expanded', 'false');
    syncSidebar();
    if (updateHash && window.location.hash !== `#${view}`) history.replaceState(null, '', `#${view}`);
  }
  function dateLabel(date, includeTime = true) {
    const value = new Date(date);
    if (Number.isNaN(value.getTime())) return '';
    const formattedDate = `${value.getDate()} ${months[value.getMonth()]}`;
    return includeTime ? `${formattedDate}, ${String(value.getHours()).padStart(2, '0')}:${String(value.getMinutes()).padStart(2, '0')}` : formattedDate;
  }
  function setAssistantState(text) { $('#assistant-state').textContent = text; }
  function setListening(listening) {
    state.listening = listening;
    $('#mic-button').setAttribute('aria-pressed', String(listening));
    $('#mic-button').setAttribute('aria-label', listening ? 'Тыңдауды тоқтату' : 'Дауыстық пәрмен беру');
    $('.orb-scene').classList.toggle('listening', listening);
    setAssistantState(listening ? 'Тыңдап тұрмын…' : 'Пәрменіңізді күтіп тұрмын');
    syncWakeActivity();
  }
  function updateSpeechToggle() {
    const button = $('#speech-toggle');
    button.setAttribute('aria-pressed', String(state.speech));
    button.querySelector('span').textContent = state.speech ? 'Дауыс қосулы' : 'Дауыс өшірулі';
    button.title = state.speech ? 'Көмекшінің дауысын өшіру' : 'Көмекшінің дауысын қосу';
  }
  function configureVoice() {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (Recognition && !state.recognition) {
      const recognition = new Recognition();
      recognition.continuous = false;
      recognition.interimResults = false;
      recognition.maxAlternatives = 1;
      recognition.onstart = () => setListening(true);
      recognition.onend = () => setListening(false);
      recognition.onerror = (event) => {
        const errors = {
          'not-allowed': 'Микрофонға рұқсат берілмеді. Параметрлерден рұқсат беріңіз немесе пәрменді жазыңыз.',
          'service-not-allowed': 'Дауыс тану қызметі қолжетімсіз. Пәрменді мәтінмен жазыңыз.',
          'audio-capture': 'Микрофон табылмады. Оның қосылғанын тексеріңіз.',
          'no-speech': 'Дауыс естілмеді. Микрофонды басып, қайта айтыңыз.',
          network: 'Дауыс тану үшін интернет байланысын тексеріңіз.',
        };
        if (event.error !== 'aborted') toast(errors[event.error] || 'Дауыс тану орындалмады. Пәрменді мәтінмен жазыңыз.', true);
        setListening(false);
      };
      recognition.onresult = (event) => {
        const text = event.results[0]?.[0]?.transcript?.trim();
        if (text) { $('#command-input').value = text; handleCommand(text); }
      };
      state.recognition = recognition;
    }
    if (state.nativeListening || wakeCommandCapture()) return;
    const native = Boolean(state.health?.desktop_mode && state.health?.native_voice_available);
    $('#mic-button').disabled = !native && !state.recognition;
    if (native) {
      $('#voice-notice').textContent = 'Қолмен пәрмен беру үшін микрофонды басып, «Айтыңыз» белгісін күтіңіз. Дауыс тануға интернет керек. Тұрақты шақыру күйі төменде көрсетілген.';
    } else if (state.recognition) {
      $('#voice-notice').textContent = 'Дауыс тану Chrome/Edge және HTTPS/localhost-та қолжетімді. Тек микрофонды басқан кезде тыңдаймын.';
    } else {
      $('#voice-notice').textContent = 'Бұл ортада дауыс тану қолжетімсіз. Пәрменді жазыңыз немесе Chrome/Edge арқылы localhost-та ашыңыз.';
    }
  }
  async function listen() {
    if (state.commandBusy || state.nativeListening) return;
    if (state.health?.desktop_mode && state.health?.native_voice_available) {
      state.nativeListening = true;
      stopSpeech();
      setListening(false);
      setAssistantState('Микрофон дайындалуда…');
      $('#voice-notice').textContent = 'Микрофон дайындалуда. «Айтыңыз» белгісі шыққанша үнсіз күтіңіз.';
      $('#mic-button').disabled = true;
      $('#mic-button').setAttribute('aria-label', 'Микрофон дайындалуда');
      let pollingActive = true;
      let pollTimer;
      let pollTimeout;
      let pollController;
      const stopPolling = () => {
        pollingActive = false;
        clearTimeout(pollTimer);
        clearTimeout(pollTimeout);
        pollController?.abort();
      };
      const pollVoiceStatus = async () => {
        if (!pollingActive) return;
        pollController = new AbortController();
        pollTimeout = setTimeout(() => pollController.abort(), 5000);
        try {
          const response = await fetch('/api/voice/status', { signal: pollController.signal });
          if (response.ok) {
            const status = await response.json();
            if (!pollingActive) return;
            const phases = {
              calibrating: '1 секунд үнсіз күтіңіз…',
              listening: 'Айтыңыз, тыңдап тұрмын…',
              recognizing: 'Дауыс қабылданды, мәтінге айналдырып жатырмын…',
            };
            const description = phases[status.phase];
            if (description) {
              setListening(status.phase === 'listening');
              setAssistantState(description);
              $('#voice-notice').textContent = description;
              $('#mic-button').setAttribute('aria-label', description);
            }
          }
        } catch { /* Readiness comes only from a successful status response. */ }
        finally {
          clearTimeout(pollTimeout);
          if (pollingActive) pollTimer = setTimeout(pollVoiceStatus, 250);
        }
      };
      try {
        await syncWakeActivity();
        await post('/api/wake/stop', {});
        const listenRequest = post('/api/voice/listen', { language: state.language }, { timeout: 45000 });
        pollVoiceStatus();
        const result = await listenRequest;
        stopPolling();
        setListening(false);
        const text = String(result.text || '').trim();
        if (text) { $('#command-input').value = text; await handleCommand(text); }
        else toast('Пәрмен анық естілмеді. Қайта айтыңыз немесе мәтінмен жазыңыз.', true);
      } catch (error) { toast(error.message, true); }
      finally {
        stopPolling(); state.nativeListening = false; setListening(false); configureVoice();
        await syncWakeActivity(true);
        if (state.settings.wake_enabled && !state.closing) {
          try { applyWakeStatus(await post('/api/wake/start', { language: state.settings.wake_language })); }
          catch (error) { toast(`Шақыруды қайта қосу мүмкін болмады: ${error.message}`, true); }
        }
      }
      return;
    }
    stopSpeech();
    if (!state.recognition) return;
    if (state.listening) { state.recognition.stop(); return; }
    try { state.recognition.lang = state.language; state.recognition.start(); }
    catch { toast('Микрофон іске қосылмады. Біраздан кейін қайталап көріңіз.', true); }
  }

  async function checkHealth() {
    try {
      state.health = await api('/api/health', { timeout: 6000 });
      $('#connection-status').classList.remove('offline');
      $('#connection-status').replaceChildren(document.createElement('span'), document.createTextNode('Жұмысқа дайын'));
      $('#sidebar-online').classList.add('connected');
      if (state.health.desktop_mode) $('#platform-label').textContent = 'Windows · Жеке көмекші';
      $('#browser-status').textContent = state.health.browser_available ? 'Браузер дайын' : 'Браузер қолжетімсіз';
      if (!state.health.browser_available) $('#browser-notice').textContent = 'Автоматтандырылған браузер осы ортада қолжетімсіз. Бағдарламаның браузер компоненті орнатылғанын тексеріңіз.';
    } catch {
      $('#connection-status').classList.add('offline');
      $('#connection-status').replaceChildren(document.createElement('span'), document.createTextNode('Байланыс жоқ'));
      $('#sidebar-online').classList.remove('connected');
    }
    configureVoice();
    renderWakeStatus();
    if (state.health?.desktop_mode && !state.wakePollRunning && !state.wakePollTimer) pollWakeStatus();
  }

  function renderEngineStatus() {
    const badge = $('#engine-status');
    let ready = false;
    let label = 'Жергілікті пәрмендер';
    let description = 'AI өшірулі. Негізгі пәрмендер жұмыс істейді.';
    if (state.settings.llm_enabled && state.settings.llm_provider === 'ollama') {
      ready = Boolean(state.ollamaStatus?.available && state.ollamaStatus?.model_ready && state.ollamaStatus.selected_model === state.settings.ollama_model);
      label = ready ? `Ollama · ${state.settings.ollama_model}` : state.ollamaChecking ? 'Ollama тексерілуде' : 'Ollama дайын емес';
      description = ready ? 'AI осы компьютердегі Ollama моделін қолданады. API кілті қажет емес.' : 'Баптаулардан Ollama-ны және орнатылған модельді тексеріңіз. API кілті керек емес.';
    } else if (state.settings.llm_enabled && state.settings.llm_provider === 'openai') {
      ready = Boolean(state.settings.llm_configured);
      label = ready ? `OpenAI · ${state.settings.llm_model}` : 'OpenAI кілті қосылмаған';
      description = ready ? 'AI ақылы OpenAI API қызметін қолданады.' : 'OpenAI қолдану үшін API кілтін қосыңыз немесе тегін Ollama-ны таңдаңыз.';
    }
    badge.classList.toggle('ai-ready', ready); badge.textContent = label; badge.title = description;
    $('#settings-status').textContent = state.settings.llm_enabled ? ready ? (state.settings.llm_provider === 'ollama' ? 'Ollama дайын' : 'OpenAI қосылған') : 'AI баптауын тексеріңіз' : 'Негізгі пәрмендер дайын';
  }
  function validOllamaModel(model) {
    return typeof model === 'string' && model.length <= 120
      && /^[A-Za-z0-9][A-Za-z0-9._-]*(?:\/[A-Za-z0-9][A-Za-z0-9._-]*)*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?$/.test(model)
      && !/(?:-cloud|:cloud)$/i.test(model);
  }
  function updateLLMProviderFields() {
    const ollama = $('#llm-provider').value === 'ollama';
    $('#ollama-fields').hidden = !ollama; $('#openai-fields').hidden = ollama;
    $('#ollama-model').required = ollama;
    $('#ollama-model').disabled = !ollama;
    $('#llm-model').required = !ollama;
    $('#llm-model').disabled = ollama;
    renderOllamaStatus();
  }
  function renderOllamaStatus() {
    const status = state.ollamaStatus;
    const model = $('#ollama-model').value.trim();
    const changed = model !== state.settings.ollama_model;
    const installed = Boolean(status?.models?.includes(model));
    const ready = Boolean(status?.available && status?.model_ready && status.selected_model === model);
    let description = 'Ollama күйі әлі тексерілмеді.';
    let error = false;
    if (state.ollamaChecking) description = 'Ollama және орнатылған модельдер тексерілуде…';
    else if (status && !status.available) { description = `Ollama іске қосылмаған немесе қолжетімсіз. ${status.error || 'Ollama-ны іске қосып, қайта тексеріңіз.'}`; error = true; }
    else if (changed) description = installed ? 'Бұл модель орнатылған. Қолдану үшін таңдауыңызды сақтаңыз.' : 'Модель таңдауы сақталмаған. Модельді жүктеп, баптауларды сақтаңыз.';
    else if (ready) description = `Дайын: ${model}. API кілті қажет емес.`;
    else if (status?.available) { description = `Ollama іске қосылған, бірақ ${model || 'таңдалған'} моделі дайын емес. Оны терминалда жүктеңіз.`; error = true; }
    $('#ollama-status').textContent = description;
    $('#ollama-status').classList.toggle('error', error);
    $('#ollama-refresh').disabled = state.ollamaChecking;
    $('#ollama-pull-command').textContent = `ollama pull ${validOllamaModel(model) ? model : 'qwen2.5:3b'}`;
    $('#ollama-setup').hidden = ready && !changed;
    $('#ollama-setup').querySelector('p').hidden = Boolean(status?.available);
  }
  async function checkOllamaStatus() {
    const id = ++state.ollamaCheckId;
    state.ollamaChecking = true; renderOllamaStatus(); renderEngineStatus();
    try {
      const result = await api('/api/ollama/status', { timeout: 12000 });
      if (id !== state.ollamaCheckId) return;
      state.ollamaStatus = {
        available: Boolean(result.available), model_ready: Boolean(result.model_ready),
        selected_model: String(result.selected_model || state.settings.ollama_model),
        models: Array.isArray(result.models) ? result.models.filter((model) => typeof model === 'string').slice(0, 200) : [],
        error: typeof result.error === 'string' ? result.error : '',
      };
      const list = $('#ollama-models'); list.replaceChildren();
      for (const model of state.ollamaStatus.models) {
        const option = document.createElement('option'); option.value = model; list.append(option);
      }
    } catch (error) {
      if (id === state.ollamaCheckId) state.ollamaStatus = { available: false, models: [], selected_model: state.settings.ollama_model, model_ready: false, error: error.message };
    } finally {
      if (id === state.ollamaCheckId) { state.ollamaChecking = false; renderOllamaStatus(); renderEngineStatus(); }
    }
  }
  function updateProviderFields() {
    const provider = $('#tts-provider').value;
    $('#edge-voice-field').hidden = provider !== 'edge';
    $('#elevenlabs-fields').hidden = provider !== 'elevenlabs';
    $('#system-voice-note').hidden = provider !== 'system';
    $('#voice-tuning').hidden = provider !== 'edge';
  }
  function acceptSettings(settings) {
    const previousModel = state.settings.ollama_model;
    const previousProvider = state.settings.llm_provider;
    const fields = ['llm_enabled', 'llm_provider', 'ollama_model', 'llm_model', 'llm_configured', 'tts_provider', 'tts_voice', 'elevenlabs_configured', 'elevenlabs_voice_id', 'elevenlabs_model', 'speech_style', 'speech_rate', 'speech_pitch', 'wake_enabled', 'wake_language'];
    for (const field of fields) if (Object.hasOwn(settings, field)) state.settings[field] = settings[field];
    $('#llm-enabled').checked = Boolean(state.settings.llm_enabled);
    $('#llm-provider').value = state.settings.llm_provider || 'ollama';
    $('#ollama-model').value = state.settings.ollama_model || 'qwen2.5:3b';
    $('#llm-model').value = state.settings.llm_model || 'gpt-4.1-mini';
    $('#tts-provider').value = state.settings.tts_provider || 'edge';
    $('#tts-voice').value = state.settings.tts_voice || 'kk-KZ-DauletNeural';
    $('#elevenlabs-voice-id').value = state.settings.elevenlabs_voice_id || '';
    $('#elevenlabs-model').value = state.settings.elevenlabs_model || 'eleven_v3';
    $('#speech-style').value = state.settings.speech_style || 'serious';
    $('#speech-rate').value = String(state.settings.speech_rate ?? -8);
    $('#speech-pitch').value = String(state.settings.speech_pitch ?? -8);
    $('#wake-enabled').checked = Boolean(state.settings.wake_enabled);
    $('#wake-language').value = state.settings.wake_language || 'kk-KZ';
    $('#llm-key-status').textContent = state.settings.llm_configured ? 'кілт сақталған' : 'қосылмаған';
    $('#elevenlabs-key-status').textContent = state.settings.elevenlabs_configured ? 'кілт сақталған' : 'қосылмаған';
    $('#clear-llm-key').checked = false;
    $('#clear-elevenlabs-key').checked = false;
    $('#clear-llm-key').disabled = !state.settings.llm_configured;
    $('#clear-elevenlabs-key').disabled = !state.settings.elevenlabs_configured;
    if (previousModel !== state.settings.ollama_model || previousProvider !== state.settings.llm_provider) state.lastEngine = null;
    updateLLMProviderFields(); updateProviderFields(); renderEngineStatus(); renderWakeStatus();
  }
  async function loadSettings() { acceptSettings(await api('/api/settings')); if (state.settings.llm_provider === 'ollama') checkOllamaStatus(); }
  async function saveSettings() {
    const result = $('#settings-result');
    result.classList.remove('error'); result.textContent = 'Баптаулар сақталуда…';
    const body = {
      llm_enabled: $('#llm-enabled').checked, llm_provider: $('#llm-provider').value,
      ollama_model: $('#llm-provider').value === 'ollama' ? $('#ollama-model').value.trim() : state.settings.ollama_model,
      llm_model: $('#llm-provider').value === 'openai' ? $('#llm-model').value.trim() : state.settings.llm_model,
      tts_provider: $('#tts-provider').value, tts_voice: $('#tts-voice').value,
      elevenlabs_voice_id: $('#elevenlabs-voice-id').value.trim(),
      elevenlabs_model: $('#elevenlabs-model').value.trim() || 'eleven_v3',
      elevenlabs_api_key: $('#elevenlabs-api-key').value.trim(), clear_elevenlabs_api_key: $('#clear-elevenlabs-key').checked,
      speech_style: $('#speech-style').value, speech_rate: Number($('#speech-rate').value), speech_pitch: Number($('#speech-pitch').value),
      wake_enabled: $('#wake-enabled').checked, wake_language: $('#wake-language').value,
    };
    if (body.llm_provider === 'openai') { body.llm_api_key = $('#llm-api-key').value.trim(); body.clear_llm_api_key = $('#clear-llm-key').checked; }
    try {
      if (!validOllamaModel(body.ollama_model)) throw new Error('Ollama моделінің дұрыс атауын жазыңыз (мысалы, qwen2.5:3b). URL қажет емес.');
      if ((body.clear_llm_api_key && body.llm_api_key) || (body.clear_elevenlabs_api_key && body.elevenlabs_api_key)) throw new Error('Кілтті жою немесе жаңарту әрекетінің бірін таңдаңыз.');
      if (!Number.isInteger(body.speech_rate) || body.speech_rate < -30 || body.speech_rate > 20 || !Number.isInteger(body.speech_pitch) || body.speech_pitch < -20 || body.speech_pitch > 10) throw new Error('Дауыс жылдамдығы −30…20, үн биіктігі −20…10 аралығында болуы керек.');
      const settings = await post('/api/settings', body);
      stopSpeech(); acceptSettings(settings);
      $('#llm-api-key').value = ''; $('#elevenlabs-api-key').value = '';
      result.textContent = 'Баптаулар сақталды. Таңдалған дауысты тексеріп көріңіз.';
      if (state.settings.llm_provider === 'ollama') checkOllamaStatus();
      if (state.health?.desktop_mode) pollWakeStatus();
    } catch (error) { result.classList.add('error'); result.textContent = error.message; throw error; }
  }

  function wakeBusy() { return Boolean(state.commandBusy || state.speechBusy || state.nativeListening || state.listening); }
  function wakeCommandCapture(status = state.wakeStatus) { return status?.phase === 'command' || (status?.phase === 'calibrating' && status?.voice_phase === 'calibrating') || status?.phase === 'recognizing'; }
  function setSpeechBusy(busy) { state.speechBusy = busy; syncWakeActivity(!busy); }
  function syncWakeActivity(force = false) {
    if (!state.health?.desktop_mode || state.closing) return Promise.resolve(true);
    if (force) state.wakeActivityForce = true;
    if (state.wakeActivityPromise) return state.wakeActivityPromise;
    if (!state.wakeActivityForce && state.wakeActivityLast === wakeBusy()) return Promise.resolve(true);
    state.wakeActivityPromise = (async () => {
      while (!state.closing && (state.wakeActivityForce || state.wakeActivityLast !== wakeBusy())) {
        const busy = wakeBusy();
        state.wakeActivityForce = false;
        try { await post('/api/wake/activity', { busy }, { timeout: 6000 }); state.wakeActivityLast = busy; }
        catch { state.wakeActivityLast = null; return false; }
      }
      return true;
    })().finally(() => { state.wakeActivityPromise = null; });
    return state.wakeActivityPromise;
  }
  function renderWakeStatus() {
    const desktop = Boolean(state.health?.desktop_mode);
    const status = state.wakeStatus;
    const phase = status?.phase;
    const enabled = Boolean(desktop && status?.enabled);
    const capturing = enabled && phase === 'command' && status.voice_phase === 'listening';
    const waiting = enabled && phase === 'waiting' && status.model_ready && !status.error;
    let title = '«Аян» деп шақыру';
    let description = desktop ? 'Шақыру күйі тексерілуде…' : 'Тұрақты шақыру үшін Windows қолданбасын ашыңыз. Браузерде микрофон батырмасын қолданыңыз.';
    if (desktop && status) {
      if (status.error) { title = 'Шақыру қолжетімсіз'; description = String(status.error); }
      else if (!status.model_ready) { title = 'Шақыру моделі қажет'; description = 'Жергілікті модельді орнатыңыз. Орнатылмайынша тұрақты тыңдау басталмайды.'; }
      else if (!enabled) { title = 'Шақыру өшірулі'; description = 'Қаласаңыз қосыңыз. Микрофон батырмасы бөлек жұмыс істейді.'; }
      else if (capturing) { title = 'Айтыңыз, тыңдап тұрмын…'; description = 'Қысқа пәрменіңіз Google дауыс тану қызметіне жіберіледі.'; }
      else if (wakeCommandCapture(status)) {
        title = status.voice_phase === 'recognizing' ? 'Пәрмен танылуда…' : 'Аян естілді. Микрофон дайындалуда…';
        description = status.voice_phase === 'calibrating' ? '1 секунд үнсіз күтіңіз. «Айтыңыз» белгісінен кейін сөйлеңіз.' : 'Дауыс қабылданғаннан кейін пәрмен орындалады.';
      } else if (waiting) { title = '«Аян» деп шақырыңыз'; description = 'Шақыру жергілікті танылады. Қарапайым әңгіме пәрменге айналмайды.'; }
      else { title = 'Шақыру уақытша үзілісте'; description = 'Көмекші жауабы немесе ағымдағы пәрмен аяқталған соң жалғасады.'; }
    }
    if (state.wakeInstalling) { title = 'Шақыру моделі орнатылуда…'; description = 'Жүктеу бірнеше минут алуы мүмкін. Орнату біткенше күтіңіз.'; }
    $('#wake-status').textContent = title;
    $('#wake-description').textContent = description;
    $('#wake-settings-status').textContent = `${title} ${status?.error ? description : ''}`;
    $('#wake-indicator').classList.toggle('active', waiting);
    $('#wake-indicator').classList.toggle('capturing', capturing);
    const wantsStop = enabled || (state.wakeInstalling && state.settings.wake_enabled);
    for (const button of [$('#wake-toggle'), $('#wake-settings-toggle')]) {
      button.textContent = wantsStop ? 'Шақыруды тоқтату' : 'Шақыруды қосу';
      button.disabled = !desktop || state.wakeChanging || (state.nativeListening && !enabled);
    }
    $('#wake-install').hidden = !desktop || Boolean(status?.model_ready);
    $('#wake-install').disabled = state.wakeInstalling;
    $('#wake-install').textContent = state.wakeInstalling ? 'Орнатылуда…' : 'Шақыру моделін орнату';
    if (enabled && wakeCommandCapture(status) && !state.nativeListening && !state.commandBusy && !state.speechBusy) {
      setAssistantState(title);
      $('#voice-notice').textContent = capturing ? title : description;
    }
  }
  function applyWakeStatus(status) {
    if (!status || typeof status.phase !== 'string') return;
    const wasCommandCapture = wakeCommandCapture();
    state.wakeStatus = status; renderWakeStatus();
    if (wasCommandCapture && !wakeCommandCapture(status) && !state.nativeListening && !state.commandBusy) {
      setAssistantState('Пәрменіңізді күтіп тұрмын'); configureVoice();
    }
  }
  async function pollWakeStatus() {
    if (!state.health?.desktop_mode || state.closing || state.wakePollRunning) return;
    clearTimeout(state.wakePollTimer); state.wakePollTimer = null; state.wakePollRunning = true;
    try {
      const status = await api(`/api/wake/status?since=${encodeURIComponent(state.wakeCursor)}`, { timeout: 7000 });
      if (state.closing) return;
      applyWakeStatus(status);
      const events = Array.isArray(status.events) ? status.events : [];
      const dispatch = state.wakePrimed;
      state.wakePrimed = true;
      for (const event of [...events].sort((a, b) => Number(a.id) - Number(b.id))) {
        const id = Number(event.id);
        if (!Number.isSafeInteger(id) || id <= state.wakeCursor) continue;
        state.wakeCursor = id;
        if (state.wakeSeen.has(id)) continue;
        state.wakeSeen.add(id);
        if (!dispatch) continue;
        if (event.type === 'wake') { stopSpeech(); applyWakeStatus(status); }
        else if (event.type === 'command' && String(event.text || '').trim()) {
          if (state.commandBusy || state.nativeListening) { toast('Ағымдағы пәрмен аяқталған соң қайта шақырыңыз.'); await syncWakeActivity(true); continue; }
          await handleCommand(String(event.text).trim());
        } else if (event.type === 'error') { toast(String(event.text || 'Шақыру пәрмені танылмады.'), true); await syncWakeActivity(true); }
      }
      if (state.wakeSeen.size > 300) state.wakeSeen = new Set([...state.wakeSeen].slice(-150));
      await syncWakeActivity();
    } catch (error) {
      if (!state.closing) { state.wakeStatus = { enabled: false, phase: 'error', model_ready: state.wakeStatus?.model_ready || false, error: error.message }; renderWakeStatus(); }
    } finally {
      state.wakePollRunning = false;
      if (!state.closing && state.health?.desktop_mode) state.wakePollTimer = setTimeout(pollWakeStatus, 1000);
    }
  }
  async function toggleWake() {
    if (!state.health?.desktop_mode || state.wakeChanging) return;
    state.wakeChanging = true;
    const enabled = !(state.wakeStatus?.enabled || (state.wakeInstalling && state.settings.wake_enabled));
    renderWakeStatus();
    try {
      acceptSettings(await post('/api/settings', { wake_enabled: enabled, wake_language: state.settings.wake_language }));
      applyWakeStatus(await post(enabled ? '/api/wake/start' : '/api/wake/stop', enabled ? { language: state.settings.wake_language } : {}));
      if (enabled) await syncWakeActivity(true);
    } catch (error) { toast(error.message, true); }
    finally { state.wakeChanging = false; renderWakeStatus(); }
  }
  async function installWakeModel() {
    if (!state.health?.desktop_mode || state.wakeInstalling) return;
    state.wakeInstalling = true; renderWakeStatus();
    try { applyWakeStatus(await post('/api/wake/model/install', {}, { timeout: 180000 })); }
    catch (error) { toast(error.message, true); }
    finally { state.wakeInstalling = false; renderWakeStatus(); }
  }

  function youtubeSearchLink(query) { return `https://www.youtube.com/results?search_query=${encodeURIComponent(query)}`; }
  function appendSearchFallback(container, query) {
    const link = document.createElement('a');
    link.className = 'results-link';
    link.href = youtubeSearchLink(query);
    link.target = '_blank'; link.rel = 'noopener noreferrer';
    link.textContent = 'YouTube-та іздеу нәтижелерін ашу ↗';
    container.append(link);
  }
  function newSessionId() {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    if (typeof crypto.getRandomValues === 'function') return Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, '0')).join('');
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  }
  function prepareListenSession(track, restart = false) {
    const previous = state.listenSession;
    if (!restart && previous && previous.video_id === track.id && !previous.ended) return previous;
    if (previous) recordListen(previous, true);
    state.listenSession = { session_id: newSessionId(), video_id: track.id, title: Array.from(String(track.title || 'YouTube бейнесі')).slice(0, 300).join(''), channel: Array.from(String(track.channel || 'YouTube')).slice(0, 200).join(''), seconds: 0, sentSeconds: 0, lastPosition: null, lastTickAt: null, pending: false, flushRequested: false, lastAttemptAt: -Infinity, ended: false };
    return state.listenSession;
  }
  function resetListeningBaseline() {
    if (state.listenSession) { state.listenSession.lastPosition = null; state.listenSession.lastTickAt = null; }
  }
  function sampleListening() {
    const session = state.listenSession;
    if (!session || !state.playerReady || !state.playing || session.ended) { resetListeningBaseline(); return; }
    let position;
    try {
      if (typeof state.player.getPlayerState === 'function' && state.player.getPlayerState() !== YT.PlayerState.PLAYING) { resetListeningBaseline(); return; }
      const videoId = state.player.getVideoData?.()?.video_id;
      if (videoId && videoId !== session.video_id) { resetListeningBaseline(); return; }
      position = Number(state.player.getCurrentTime?.());
    } catch { resetListeningBaseline(); return; }
    if (!Number.isFinite(position) || position < 0) { resetListeningBaseline(); return; }
    const now = performance.now();
    if (session.lastPosition !== null && session.lastTickAt !== null) {
      const delta = position - session.lastPosition;
      const elapsed = (now - session.lastTickAt) / 1000;
      // A seek, buffering gap, or a throttled tab must not turn into listening time.
      if (elapsed > 0 && elapsed <= 3 && delta > 0 && delta <= Math.min(3, elapsed * 1.5 + 0.25)) session.seconds += Math.min(delta, elapsed);
    }
    session.lastPosition = position; session.lastTickAt = now;
    recordListen(session);
  }
  async function recordListen(session, force = false, keepalive = false) {
    const seconds = Math.floor(session.seconds);
    if (seconds < 30 || seconds <= session.sentSeconds || (!force && seconds - session.sentSeconds < 30)) return;
    if (session.pending) { if (force) session.flushRequested = true; return; }
    if (!force && performance.now() - session.lastAttemptAt < 5000) return;
    session.pending = true; session.lastAttemptAt = performance.now();
    try {
      await post('/api/music/listens', { video_id: session.video_id, title: session.title, channel: session.channel, session_id: session.session_id, listened_seconds: seconds }, { keepalive, timeout: 12000 });
      session.sentSeconds = Math.max(session.sentSeconds, seconds);
      if (!state.closing) await loadPlaylist();
    } catch { if (!state.closing) $('#playlist-notice').textContent = 'Тыңдалым есебін сақтау мүмкін болмады. Тыңдауды жалғастырсаңыз, қайта сақтап көремін.'; }
    finally {
      session.pending = false;
      if (session.flushRequested) { session.flushRequested = false; recordListen(session, true, keepalive); }
    }
  }
  function renderPlaylist() {
    const container = $('#playlist-tracks'); container.replaceChildren();
    $('#playlist-count').textContent = `${state.playlist.length} ән`;
    $('#playlist-play').disabled = !state.playlist.length;
    $('#playlist-next').disabled = !state.playlistQueueActive || state.playlistQueueIndex + 1 >= state.playlistQueue.length;
    if (!state.playlist.length) {
      const empty = document.createElement('div'); empty.className = 'empty-state';
      const text = document.createElement('p'); text.textContent = 'Әзірге бос. Әнді кемінде 30 секунд тыңдаңыз — плейлист өзі жиналады.';
      empty.append(text); container.append(empty); return;
    }
    for (const track of state.playlist) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'track-result';
      button.classList.toggle('active', state.currentTrack?.id === track.id);
      const image = document.createElement('img'); image.alt = ''; image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
      image.src = `https://i.ytimg.com/vi/${encodeURIComponent(track.id)}/default.jpg`;
      const details = document.createElement('span');
      const title = document.createElement('strong'); title.textContent = track.title || 'YouTube бейнесі';
      const channel = document.createElement('small'); channel.textContent = track.channel || 'YouTube';
      const count = document.createElement('span'); count.className = 'play-count'; count.textContent = `${Number(track.play_count) || 0} рет`;
      details.append(title, channel); button.append(image, details, count, icons('play'));
      button.addEventListener('click', () => selectTrack(track).catch((error) => toast(error.message, true)));
      container.append(button);
    }
  }
  async function loadPlaylist() {
    const result = await api('/api/music/playlist', { timeout: 12000 });
    state.playlist = (Array.isArray(result.tracks) ? result.tracks : []).filter((track) => /^[A-Za-z0-9_-]{11}$/.test(String(track.id || '')));
    renderPlaylist();
  }
  async function playPlaylist() {
    if (!state.playlist.length) await loadPlaylist();
    if (!state.playlist.length) throw new Error('Жиі тыңдалатын әндер тізімі әзірге бос. Алдымен әнді кемінде 30 секунд тыңдаңыз.');
    state.playlistQueue = [...state.playlist]; state.playlistQueueIndex = 0; state.playlistQueueActive = true;
    showView('music');
    try { await selectTrack(state.playlistQueue[0], { fromQueue: true }); }
    catch (error) { state.playlistQueueActive = false; renderPlaylist(); throw error; }
    $('#playlist-notice').textContent = `Плейлист: 1/${state.playlistQueue.length}. Келесі ән алдыңғысы аяқталған соң қосылады.`;
    renderPlaylist();
    return 'Жиі тыңдалатын әндер плейлисті таңдалды. Музыка басталмаса, ойнату батырмасын басыңыз.';
  }
  async function nextPlaylistTrack() {
    if (!state.playlistQueueActive || state.playlistQueueIndex + 1 >= state.playlistQueue.length) throw new Error('Плейлистте келесі ән жоқ.');
    state.playlistQueueIndex += 1;
    try { await selectTrack(state.playlistQueue[state.playlistQueueIndex], { fromQueue: true }); }
    catch (error) { state.playlistQueueActive = false; renderPlaylist(); throw error; }
    $('#playlist-notice').textContent = `Плейлист: ${state.playlistQueueIndex + 1}/${state.playlistQueue.length}.`;
    renderPlaylist();
    return 'Плейлисттегі келесі ән таңдалды.';
  }
  function handleMusicState(event) {
    const playing = event.data === YT.PlayerState.PLAYING;
    if (!playing && state.listenSession) { recordListen(state.listenSession, true); resetListeningBaseline(); }
    updatePlayback(playing);
    if (playing) {
      if (state.currentTrack) prepareListenSession(state.currentTrack);
      sampleListening();
      $('#music-notice').textContent = 'Ойнап жатыр. Дыбыс деңгейін өзгерте аласыз.';
    }
    if (event.data === YT.PlayerState.ENDED) {
      if (state.listenSession) state.listenSession.ended = true;
      $('#music-notice').textContent = 'Әуен аяқталды. Басқа әнді таңдаңыз.';
      if (state.playlistQueueActive && state.playlistQueueIndex + 1 < state.playlistQueue.length) nextPlaylistTrack().catch((error) => toast(error.message, true));
      else if (state.playlistQueueActive) { state.playlistQueueActive = false; $('#playlist-notice').textContent = 'Плейлист аяқталды.'; renderPlaylist(); }
    }
  }
  async function searchMusic(query, autoSelect = false, strict = false) {
    query = String(query || '').trim();
    if (!query) throw new Error('Ән немесе орындаушы атауын енгізіңіз.');
    $('#music-query').value = query;
    $('#music-notice').textContent = 'Әуендерді іздеп жатырмын…';
    const results = $('#music-results');
    results.replaceChildren();
    let response;
    try { response = await api(`/api/music/search?q=${encodeURIComponent(query)}`, { timeout: 25000 }); }
    catch (error) {
      $('#music-notice').textContent = error.message;
      appendSearchFallback(results, query);
      if (strict) throw error;
      return 'Іздеу қолжетімсіз. YouTube-та іздеу сілтемесін ашуға болады.';
    }
    const tracks = Array.isArray(response.tracks) ? response.tracks : [];
    const validTracks = [];
    for (const track of tracks) {
      if (!/^[A-Za-z0-9_-]{11}$/.test(String(track.id || ''))) continue;
      validTracks.push(track);
      const button = document.createElement('button');
      button.className = 'track-result'; button.type = 'button';
      const image = document.createElement('img');
      image.alt = ''; image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
      try {
        const thumbnail = new URL(track.thumbnail);
        if (thumbnail.protocol === 'https:') image.src = thumbnail.href;
      } catch { /* Missing thumbnail is harmless. */ }
      const details = document.createElement('span');
      const title = document.createElement('strong'); title.textContent = track.title || 'YouTube бейнесі';
      const channel = document.createElement('small'); channel.textContent = track.channel || 'YouTube';
      details.append(title, channel); button.append(image, details, icons('play'));
      button.addEventListener('click', () => selectTrack(track, { candidates: validTracks }).catch((error) => toast(error.message, true)));
      results.append(button);
    }
    appendSearchFallback(results, query);
    if (!tracks.length || results.children.length === 1) {
      $('#music-notice').textContent = response.error || 'Нәтиже табылмады. YouTube-та іздеуді жалғастыра аласыз.';
      if (strict) throw new Error($('#music-notice').textContent);
      return 'Музыка нәтижесі табылмады. YouTube-та іздеу сілтемесі дайын.';
    }
    $('#music-notice').textContent = 'Әнді таңдап ойнатыңыз. Автоматты ойнату үшін қосымша басу қажет болуы мүмкін.';
    if (autoSelect) {
      await selectTrack(validTracks[0], { candidates: validTracks });
      const trackName = validTracks[0].title || query;
      return state.playing ? `Ойнап жатыр: ${trackName}.` : `Таңдалған ән: ${trackName}. Басталмаса, ойнату батырмасын басыңыз.`;
    }
    return `${results.children.length - 1} әуен табылды. Тізімнен тыңдағыңыз келетінін таңдаңыз.`;
  }
  function loadYouTube() {
    if (window.YT?.Player) return Promise.resolve();
    if (state.youtubePromise) return state.youtubePromise;
    state.youtubePromise = new Promise((resolve, reject) => {
      const timer = setTimeout(() => { state.youtubePromise = null; document.getElementById('youtube-api-script')?.remove(); reject(new Error('YouTube ойнатқышы жүктелмеді. YouTube-та ашу сілтемесін пайдаланыңыз.')); }, 18000);
      window.onYouTubeIframeAPIReady = () => { clearTimeout(timer); resolve(); };
      let script = document.getElementById('youtube-api-script');
      if (!script) {
        script = document.createElement('script'); script.id = 'youtube-api-script';
        script.src = 'https://www.youtube.com/iframe_api'; script.async = true;
        script.onerror = () => { clearTimeout(timer); state.youtubePromise = null; script.remove(); reject(new Error('YouTube-қа қосылу мүмкін болмады. Интернет байланысын тексеріңіз.')); };
        document.head.append(script);
      }
    });
    return state.youtubePromise;
  }
  function updatePlayback(playing) {
    state.playing = playing;
    $('#play-toggle').replaceChildren(icons(playing ? 'pause' : 'play'));
    $('#play-toggle').setAttribute('aria-label', playing ? 'Музыканы тоқтата тұру' : 'Музыканы ойнату');
  }
  function resetUnreadyPlayer() {
    if (state.playerReady) return;
    try { state.player?.destroy(); } catch { /* An incomplete iframe can still be removed safely. */ }
    state.player = null;
    document.getElementById('youtube-player')?.remove();
    const container = document.createElement('div'); container.id = 'youtube-player';
    $('#player-stage').append(container);
    $('#player-stage').classList.remove('has-video');
    $('#play-toggle').disabled = true;
    updatePlayback(false);
  }
  async function recoverMusic(errorCode) {
    if (state.musicRecoveryBusy || !state.currentTrack) return;
    if (errorCode === 153) {
      updatePlayback(false);
      if (state.listenSession) { recordListen(state.listenSession, true); resetListeningBaseline(); }
      state.playlistQueueActive = false; renderPlaylist();
      $('#music-notice').textContent = 'YouTube бұл кірістірілген ойнатқышты қабылдамады (153). YouTube-та ашып көріңіз немесе қолданбаны жаңартыңыз.';
      return;
    }
    state.musicRecoveryBusy = true;
    const failed = state.currentTrack;
    state.failedMusicIds.add(failed.id);
    updatePlayback(false);
    if (state.listenSession) { recordListen(state.listenSession, true); resetListeningBaseline(); }
    $('#music-notice').textContent = `Бейне Аян ішінде ашылмады (${errorCode}). Басқа нұсқасын іздеп жатырмын…`;
    try {
      let alternatives = state.musicCandidates;
      if (!alternatives.length) {
        if (!failed.title || failed.title === 'YouTube бейнесі' || failed.title === 'YouTube') {
          state.playlistQueueActive = false; renderPlaylist();
          $('#music-notice').textContent = 'Бұл сілтемедегі бейнені Аян ішінде ашуға болмайды. YouTube-та ашу сілтемесін пайдаланыңыз.';
          return;
        }
        const response = await api(`/api/music/search?q=${encodeURIComponent(`${failed.title || ''} ${failed.channel || ''}`.trim())}`, { timeout: 25000 });
        alternatives = Array.isArray(response.tracks) ? response.tracks : [];
      }
      const next = alternatives.find((track) => /^[A-Za-z0-9_-]{11}$/.test(String(track.id || '')) && !state.failedMusicIds.has(track.id));
      if (next) {
        await selectTrack(next, { fromQueue: state.playlistQueueActive, candidates: alternatives, recovering: true });
        $('#music-notice').textContent = `Алдыңғы бейнені кірістіруге болмайды. Басқа нұсқа таңдалды: ${next.title || 'YouTube бейнесі'}.`;
      } else {
        state.playlistQueueActive = false; renderPlaylist();
        $('#music-notice').textContent = 'Бұл әннің Аян ішінде ойнатылатын нұсқасы табылмады. YouTube-та ашу сілтемесін пайдаланыңыз.';
      }
    } catch (error) {
      state.playlistQueueActive = false; renderPlaylist();
      $('#music-notice').textContent = `Басқа нұсқаны іздеу мүмкін болмады: ${error.message}`;
    } finally { state.musicRecoveryBusy = false; }
  }
  async function selectTrack(track, { fromQueue = false, candidates = [], recovering = false } = {}) {
    if (!/^[A-Za-z0-9_-]{11}$/.test(String(track?.id || ''))) throw new Error('YouTube бейне идентификаторы жарамсыз.');
    if (!recovering) state.failedMusicIds.clear();
    state.musicCandidates = candidates;
    if (!fromQueue) { state.playlistQueueActive = false; state.playlistQueueIndex = -1; }
    prepareListenSession(track, true); resetListeningBaseline(); updatePlayback(false);
    state.currentTrack = track;
    renderPlaylist();
    $('#track-title').textContent = track.title || 'YouTube бейнесі';
    $('#track-channel').textContent = track.channel || 'YouTube';
    $('#youtube-link').href = `https://www.youtube.com/watch?v=${encodeURIComponent(track.id)}`;
    $('#music-notice').textContent = 'Ойнатқышты дайындап жатырмын…';
    try {
      await loadYouTube();
      $('#player-stage').classList.add('has-video');
      if (state.player && state.playerReady) {
        state.player.loadVideoById(track.id);
        state.player.setVolume(state.volume);
      } else if (!state.player) {
        await new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error('Ойнатқыш дайын болмады. YouTube-та ашу сілтемесін пайдаланыңыз.')), 18000);
          let initializingPlayer;
          initializingPlayer = new YT.Player('youtube-player', {
            videoId: track.id, width: '100%', height: '100%',
            playerVars: { playsinline: 1, rel: 0, autoplay: 0, origin: window.location.origin },
            events: {
              onReady: () => { if (state.player !== initializingPlayer) return; clearTimeout(timer); state.playerReady = true; state.player.setVolume(state.volume); $('#play-toggle').disabled = false; if (state.currentTrack?.id !== track.id) state.player.loadVideoById(state.currentTrack.id); else state.player.playVideo(); resolve(); },
              onStateChange: handleMusicState,
              onError: (event) => {
                if (!state.playerReady) {
                  clearTimeout(timer);
                  reject(new Error('YouTube ойнатқышы дайын болмады. YouTube-та ашу сілтемесін пайдаланыңыз.'));
                  return;
                }
                recoverMusic(event.data);
              },
              onAutoplayBlocked: () => { updatePlayback(false); resetListeningBaseline(); $('#music-notice').textContent = 'Ойнату батырмасын басыңыз: автоматты ойнатуға рұқсат берілмеді.'; },
            },
          });
          state.player = initializingPlayer;
        });
      }
      $('#music-notice').textContent = state.playing ? 'Ойнап жатыр. Жақсы тыңдалым!' : 'Ән дайын. Ойнату батырмасын басыңыз.';
      $('#play-toggle').disabled = !state.playerReady;
    } catch (error) { resetUnreadyPlayer(); $('#music-notice').textContent = error.message; throw error; }
  }
  function setVolume(value) {
    state.volume = clampVolume(value);
    $('#volume-slider').value = String(state.volume);
    $('#volume-value').textContent = `${state.volume}%`;
    remember('ayan-volume', state.volume);
    if (state.playerReady) state.player.setVolume(state.volume);
    if (state.speechAudio) state.speechAudio.volume = state.volume / 100;
  }
  function controlMusic(payload) {
    if (payload.action === 'volume') {
      setVolume(payload.volume);
      return `Дыбыс деңгейі ${state.volume}%.`;
    }
    if (!state.playerReady) return 'Алдымен ән іздеп, тізімнен тыңдайтын әуенді таңдаңыз.';
    if (payload.action === 'pause') { state.player.pauseVideo(); updatePlayback(false); return 'Музыка кідіртілді.'; }
    state.player.playVideo();
    return 'Ойнатуды сұрадым. Әуен басталмаса, ойнату батырмасын басыңыз.';
  }

  function safeAnimeUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && url.hostname === 'animego.work' && !url.port && !url.username && !url.password ? url.href : null;
    } catch { return null; }
  }
  async function displayAnimePage(url, notice) {
    $('#anime-external').href = url;
    $('.anime-frame-wrap').hidden = false;
    $('#anime-frame').src = url;
    $('#anime-notice').textContent = `${notice} Сайт бейнені Аян ішінде көрсетуге рұқсат бермесе, «Сайтта ашу» сілтемесін қолданыңыз.`;
  }
  async function openAnime(result, season, episode) {
    const seriesUrl = safeAnimeUrl(result.url);
    if (!seriesUrl) throw new Error('Аниме сілтемесі жарамсыз.');
    let selectedUrl = seriesUrl;
    let notice = 'Аниме беті ашылды. Ойнату батырмасын сайттың ішінен басыңыз.';
    $('#anime-episodes').replaceChildren();
    try {
      const response = await api(`/api/anime/episodes?url=${encodeURIComponent(seriesUrl)}`, { timeout: 16000 });
      const episodes = Array.isArray(response.episodes) ? response.episodes : [];
      for (const item of episodes) {
        const url = safeAnimeUrl(item.url);
        if (!url) continue;
        const button = document.createElement('button');
        button.className = 'secondary-button'; button.type = 'button';
        button.textContent = `${item.season}-маусым · ${item.episode}-серия`;
        button.addEventListener('click', () => displayAnimePage(url, `${button.textContent} беті ашылды. Сайттағы ойнату батырмасын басыңыз.`).catch((error) => toast(error.message, true)));
        $('#anime-episodes').append(button);
      }
      if (episode) {
        const match = episodes.find((item) => item.episode === episode && (!season || item.season === season) && safeAnimeUrl(item.url));
        if (match) {
          selectedUrl = safeAnimeUrl(match.url);
          notice = `${match.season}-маусым, ${match.episode}-серия беті ашылды. Сайттағы ойнату батырмасын басыңыз.`;
        } else {
          notice = `Сұралған ${season || 1}-маусым, ${episode}-серияға тікелей сілтеме табылмады. Аниме беті ашылды; серияны сайттан таңдаңыз.`;
        }
      }
    } catch (error) {
      notice = `Серия тізімі жүктелмеді: ${error.message} Аниме бетінен серияны таңдаңыз.`;
    }
    await displayAnimePage(selectedUrl, notice);
    return notice;
  }
  async function searchAnime(query, season = null, episode = null) {
    query = String(query || '').trim();
    if (!query) throw new Error('Аниме атауын енгізіңіз.');
    $('#anime-query').value = query;
    $('#anime-season').value = season || '';
    $('#anime-episode').value = episode || '';
    $('#anime-notice').textContent = 'Аниме ізделіп жатыр…';
    let response;
    try { response = await api(`/api/anime/search?q=${encodeURIComponent(query)}`, { timeout: 28000 }); }
    catch (error) {
      const searchUrl = `https://animego.work/index.php?do=search&subaction=search&story=${encodeURIComponent(query)}`;
      await displayAnimePage(searchUrl, `${error.message} Сайттағы іздеу беті ашылды; нәтижені қолмен таңдаңыз.`);
      return $('#anime-notice').textContent;
    }
    const results = (Array.isArray(response.results) ? response.results : []).filter((item) => safeAnimeUrl(item.url));
    $('#anime-results').replaceChildren();
    for (const item of results) {
      const button = document.createElement('button');
      button.className = 'track-result'; button.type = 'button';
      const title = document.createElement('strong'); title.textContent = item.title || 'Аниме';
      button.append(icons('play'), title);
      button.addEventListener('click', () => openAnime(item, season, episode).catch((error) => toast(error.message, true)));
      $('#anime-results').append(button);
    }
    if (!results.length) {
      $('#anime-notice').textContent = 'Аниме табылмады. Атауын тексеріп, қайта іздеңіз.';
      return 'Аниме табылмады. Атауын тексеріп, қайта іздеңіз.';
    }
    return openAnime(results[0], season, episode);
  }

  function confirmClick(target) {
    return new Promise((resolve) => {
      const dialog = $('#action-dialog');
      $('#dialog-description').textContent = `Аян «${target || 'таңдалған элемент'}» элементін басады.`;
      dialog.returnValue = 'cancel';
      dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
      dialog.showModal();
    });
  }
  async function browserAction(payload, stopOnCancel = false) {
    if (state.browserBusy) throw new Error('Алдыңғы браузер әрекеті орындалып жатыр. Аяқталғанын күтіңіз.');
    const request = { ...payload };
    if (request.action === 'click') {
      if (!(await confirmClick(request.target))) {
        if (stopOnCancel) throw new Error('Басу әрекетінен бас тартылды. Кейінгі қадамдар орындалмады.');
        return 'Басу әрекетінен бас тартылды.';
      }
      request.confirmed = true;
    }
    state.browserBusy = true;
    $('#browser-notice').textContent = 'Браузер әрекетін орындап жатырмын…';
    const controls = $$('#browser-open-form button, #browser-action-form button, #browser-back');
    controls.forEach((control) => { control.disabled = true; });
    try {
      const result = await post('/api/browser/action', request);
      if (result.url) $('#browser-url').value = result.url;
      $('#browser-title').textContent = result.title || result.url || 'Ашылған бет';
      if (result.screenshot) {
        const img = $('#browser-screenshot');
        const screenshot = String(result.screenshot);
        if (/^[A-Za-z0-9+/=\s]+$/.test(screenshot)) {
          img.src = `data:image/png;base64,${screenshot}`;
          img.hidden = false;
          $('.browser-empty').hidden = true;
        }
      }
      $('#browser-status').textContent = 'Браузер дайын';
      $('#browser-notice').textContent = result.message || 'Әрекет орындалды. Ашылған беттің суреті жаңартылды.';
      return result.message || 'Браузер әрекеті орындалды.';
    } catch (error) { $('#browser-notice').textContent = error.message; throw error; }
    finally { state.browserBusy = false; controls.forEach((control) => { control.disabled = false; }); }
  }
  function updateBrowserActionFields() {
    const action = $('#browser-action').value;
    $('.target-field').hidden = action === 'scroll';
    $('.value-field').hidden = action !== 'fill';
    $('.direction-field').hidden = action !== 'scroll';
    $('#browser-target').required = action !== 'scroll';
    $('#browser-value').required = action === 'fill';
  }

  function renderDiary() {
    const container = $('#diary-entries');
    container.replaceChildren();
    $('#diary-total').textContent = `${state.entries.length} жазба`;
    $('#diary-count').textContent = String(state.entries.length);
    if (!state.entries.length) {
      const empty = document.createElement('div'); empty.className = 'empty-state';
      const symbol = document.createElement('span'); symbol.className = 'empty-symbol'; symbol.textContent = '✦';
      const title = document.createElement('h3'); title.textContent = 'Әр күннің өз оқиғасы бар';
      const description = document.createElement('p'); description.textContent = 'Алғашқы ойыңызды осы жерге сақтаңыз.';
      empty.append(symbol, title, description); container.append(empty); return;
    }
    for (const entry of state.entries) {
      const article = document.createElement('article'); article.className = 'diary-entry';
      const header = document.createElement('div'); header.className = 'diary-entry-header';
      const time = document.createElement('time'); time.dateTime = entry.created_at || ''; time.textContent = dateLabel(entry.created_at);
      const remove = document.createElement('button'); remove.className = 'icon-button'; remove.type = 'button';
      remove.setAttribute('aria-label', 'Осы жазбаны жою'); remove.append(icons('trash'));
      remove.addEventListener('click', async () => {
        remove.disabled = true;
        try { await api(`/api/diary/${encodeURIComponent(entry.id)}`, { method: 'DELETE' }); await loadDiary(); toast('Жазба жойылды.'); }
        catch (error) { toast(error.message, true); remove.disabled = false; }
      });
      const content = document.createElement('p'); content.textContent = entry.content || '';
      header.append(time, remove); article.append(header, content); container.append(article);
    }
  }
  async function loadDiary() {
    const result = await api('/api/diary');
    state.entries = Array.isArray(result.entries) ? result.entries : [];
    renderDiary();
  }
  function renderReminders() {
    const container = $('#reminder-list'); container.replaceChildren();
    const active = state.reminders.filter((reminder) => !reminder.completed).length;
    $('#reminder-total').textContent = `${active} белсенді`;
    if (!state.reminders.length) {
      const empty = document.createElement('div'); empty.className = 'empty-state';
      const text = document.createElement('p'); text.textContent = 'Қазір бәрі тыныш. Бірінші еске салғышты қосыңыз.';
      empty.append(text); container.append(empty); return;
    }
    const sorted = [...state.reminders].sort((a, b) => Number(a.completed) - Number(b.completed) || new Date(a.due_at) - new Date(b.due_at));
    for (const reminder of sorted) {
      const due = !reminder.completed && new Date(reminder.due_at).getTime() <= Date.now();
      const item = document.createElement('div'); item.className = `reminder-item${reminder.completed ? ' completed' : ''}${due ? ' due' : ''}`;
      const check = document.createElement('button'); check.className = 'reminder-check'; check.type = 'button'; check.disabled = Boolean(reminder.completed);
      check.setAttribute('aria-label', reminder.completed ? 'Орындалған' : 'Орындалды деп белгілеу');
      if (reminder.completed) check.append(icons('check'));
      check.addEventListener('click', async () => {
        check.disabled = true;
        try { await post(`/api/reminders/${encodeURIComponent(reminder.id)}/complete`, {}); await loadReminders(); }
        catch (error) { toast(error.message, true); check.disabled = false; }
      });
      const details = document.createElement('div');
      const title = document.createElement('strong'); title.textContent = reminder.title || 'Еске салғыш';
      const time = document.createElement('small'); time.textContent = reminder.completed ? 'Орындалды' : dateLabel(reminder.due_at);
      details.append(title, time); item.append(check, details);
      if (due) {
        const badge = document.createElement('span'); badge.className = 'due-badge'; badge.textContent = 'Уақыты келді'; item.append(badge);
        const reminderKey = String(reminder.id);
        if (!state.notifiedReminders.has(reminderKey)) {
          state.notifiedReminders.add(reminderKey);
          const notification = `Еске салу: ${reminder.title}`;
          toast(notification, false, true); speak(notification);
        }
      }
      container.append(item);
    }
  }
  async function loadReminders() {
    const result = await api('/api/reminders');
    state.reminders = Array.isArray(result.reminders) ? result.reminders : [];
    renderReminders();
  }

  async function executeIntent(intent, payload = {}, reply = 'Пәрмен орындалды.', inPlan = false) {
    const serverIntents = ['diary_add', 'diary_list', 'reminder_add', 'reminders_list', 'time'];
    if (inPlan && serverIntents.includes(intent)) {
      const result = await post('/api/assistant/action', { intent, payload });
      payload = result.payload || payload;
      reply = result.message || reply;
    }
    switch (intent) {
      case 'music_search':
        showView('music'); return searchMusic(payload.query, true, inPlan);
      case 'anime_search':
        showView('anime'); return searchAnime(payload.query, payload.season, payload.episode);
      case 'music_control':
        showView('music');
        if (inPlan && payload.action !== 'volume' && !state.playerReady) throw new Error('Алдымен әнді таңдап, ойнатқыштың дайын болғанын күтіңіз.');
        return controlMusic(payload);
      case 'music_playlist':
        showView('music'); await loadPlaylist();
        if (payload.action === 'show') return state.playlist.length ? `Жиі тыңдалған ${state.playlist.length} ән көрсетілді.` : 'Жиі тыңдалатын әндер тізімі әзірге бос. Әнді кемінде 30 секунд тыңдаңыз.';
        return playPlaylist();
      case 'browser_open':
        showView('browser'); return browserAction({ action: 'open', url: payload.url }, inPlan);
      case 'browser_action':
        showView('browser'); return browserAction(payload, inPlan);
      case 'diary_add':
      case 'diary_list':
        showView('diary'); await loadDiary(); return reply;
      case 'reminder_add':
      case 'reminders_list':
        showView('reminders'); await loadReminders(); return reply;
      case 'time':
        if (payload.current_time) return `Қазір сағат ${new Date(payload.current_time).toLocaleTimeString('kk-KZ', { hour: '2-digit', minute: '2-digit' })}.`;
        return reply;
      default:
        if (inPlan) throw new Error('Бұл жоспар қадамы қолдау таппайды.');
        return reply;
    }
  }
  async function executePlan(result) {
    const actions = result.payload?.actions;
    const allowed = ['music_search', 'anime_search', 'music_control', 'music_playlist', 'browser_open', 'browser_action', 'diary_add', 'diary_list', 'reminder_add', 'reminders_list', 'time'];
    if (!Array.isArray(actions) || !actions.length || actions.length > 6) throw new Error('Жоспарда 1–6 қадам болуы керек. Сұрағыңызды шағын бөліктерге бөліңіз.');
    if (actions.some((action) => !action || !allowed.includes(action.intent) || !action.payload || typeof action.payload !== 'object' || Array.isArray(action.payload))) throw new Error('Жоспар қадамының пішімі жарамсыз. Қайта сұрап көріңіз.');
    for (let index = 0; index < actions.length; index += 1) {
      const action = actions[index];
      setAssistantState(`${index + 1}/${actions.length} қадам орындалуда…`);
      try {
        const reply = await executeIntent(action.intent, action.payload, 'Қадам орындалды.', true);
        message(`${index + 1}. ${reply}`, 'assistant', false, false);
      } catch (error) {
        throw new Error(`${index + 1}-қадам тоқтатылды: ${error.message} Қалған қадамдар орындалмады.`);
      }
    }
    return result.message || `${actions.length} қадам аяқталды.`;
  }
  async function handleCommand(text) {
    text = String(text || '').trim();
    if (!text || state.commandBusy) return;
    state.commandBusy = true;
    const send = $('.send-button'); send.disabled = true;
    const history = state.history.slice(-8).map(({ role, content }) => ({ role, content }));
    stopSpeech();
    message(text, 'user'); $('#command-input').value = '';
    setAssistantState('Ойыңызды әрекетке айналдырамын…');
    $('#command-progress').hidden = false;
    $('#command-progress').textContent = state.settings.llm_provider === 'ollama' && state.settings.llm_enabled ? 'Жергілікті көмекші жауап дайындауда. Модельдің алғашқы жүктелуі бірнеше минут алуы мүмкін…' : 'Көмекші пәрменіңізді өңдеп жатыр…';
    try {
      await syncWakeActivity();
      const result = await post('/api/command', { text, volume: state.volume, language: state.language, history }, { timeout: state.settings.llm_provider === 'ollama' ? 210000 : 45000 });
      state.lastEngine = result.engine || null; renderEngineStatus();
      const reply = result.intent === 'plan' ? await executePlan(result) : await executeIntent(result.intent, result.payload || {}, result.message || 'Пәрмен қабылданды.');
      message(reply);
    } catch (error) { message(error.message, 'assistant', true); }
    finally { state.commandBusy = false; send.disabled = false; $('#command-progress').hidden = true; setAssistantState(state.listening ? 'Тыңдап тұрмын…' : 'Пәрменіңізді күтіп тұрмын'); await syncWakeActivity(true); }
  }
  async function withFormBusy(form, operation) {
    const button = form.querySelector('button[type="submit"]');
    if (button?.disabled) return;
    if (button) button.disabled = true;
    try { await operation(); } catch (error) { toast(error.message, true); }
    finally { if (button) button.disabled = false; }
  }

  $$('.nav-item').forEach((button) => button.addEventListener('click', () => showView(button.dataset.view)));
  $('.brand').addEventListener('click', (event) => { event.preventDefault(); showView('home'); });
  window.addEventListener('hashchange', () => showView(window.location.hash.slice(1), false));
  $('#menu-toggle').addEventListener('click', () => {
    const open = $('#sidebar').classList.toggle('open'); $('#menu-toggle').setAttribute('aria-expanded', String(open)); syncSidebar();
  });
  document.addEventListener('click', (event) => {
    if ($('#sidebar').classList.contains('open') && !$('#sidebar').contains(event.target) && !$('#menu-toggle').contains(event.target)) {
      $('#sidebar').classList.remove('open'); $('#menu-toggle').setAttribute('aria-expanded', 'false'); syncSidebar();
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { $('#sidebar').classList.remove('open'); $('#menu-toggle').setAttribute('aria-expanded', 'false'); syncSidebar(); }
  });
  mobileMedia.addEventListener('change', syncSidebar);
  $('#command-form').addEventListener('submit', (event) => { event.preventDefault(); handleCommand($('#command-input').value); });
  $$('[data-command]').forEach((button) => button.addEventListener('click', () => handleCommand(button.dataset.command)));
  $('#help-button').addEventListener('click', () => handleCommand('Не істей аласың?'));
  $('#mic-button').addEventListener('click', listen);
  $('#voice-language').value = ['kk-KZ', 'ru-RU'].includes(state.language) ? state.language : 'kk-KZ';
  state.language = $('#voice-language').value;
  $('#voice-language').addEventListener('change', (event) => { state.language = event.target.value; remember('ayan-language', state.language); });
  $('#speech-toggle').addEventListener('click', () => {
    state.speech = !state.speech; remember('ayan-speech', state.speech); updateSpeechToggle();
    if (!state.speech) stopSpeech();
  });
  $('#speech-stop').addEventListener('click', () => stopSpeech('Дауыс тоқтатылды.'));
  $('#speech-retry').addEventListener('click', () => {
    if (!state.speech) return;
    if (state.speechAudio) playSpeechAudio(state.speechAudio, state.speechGeneration);
    else if (state.lastSpeechText) speak(state.lastSpeechText, state.lastSpeechLanguage);
  });
  $('#tts-provider').addEventListener('change', updateProviderFields);
  $('#llm-provider').addEventListener('change', () => { updateLLMProviderFields(); if ($('#llm-provider').value === 'ollama') checkOllamaStatus(); });
  $('#ollama-model').addEventListener('input', renderOllamaStatus);
  $('#ollama-refresh').addEventListener('click', checkOllamaStatus);
  $('#speech-style').addEventListener('change', (event) => {
    const serious = event.target.value === 'serious';
    $('#speech-rate').value = serious ? '-8' : '0';
    $('#speech-pitch').value = serious ? '-8' : '0';
    if (serious) $('#tts-voice').value = 'kk-KZ-DauletNeural';
  });
  $('#wake-toggle').addEventListener('click', toggleWake);
  $('#wake-settings-toggle').addEventListener('click', toggleWake);
  $('#wake-install').addEventListener('click', installWakeModel);
  $('#settings-form').addEventListener('submit', (event) => {
    event.preventDefault(); withFormBusy(event.currentTarget, saveSettings);
  });
  $('#voice-test').addEventListener('click', () => {
    if (!state.speech) { state.speech = true; remember('ayan-speech', true); updateSpeechToggle(); }
    speak('Сәлем! Мен Аянмын. Сізге қазақ тілінде көмектесуге дайынмын.', 'kk-KZ');
  });
  $('#music-search-form').addEventListener('submit', (event) => {
    event.preventDefault(); withFormBusy(event.currentTarget, () => searchMusic($('#music-query').value));
  });
  $('#anime-search-form').addEventListener('submit', (event) => {
    event.preventDefault();
    withFormBusy(event.currentTarget, () => searchAnime($('#anime-query').value,
      $('#anime-season').value ? Number($('#anime-season').value) : null,
      $('#anime-episode').value ? Number($('#anime-episode').value) : null));
  });
  $('#play-toggle').addEventListener('click', () => controlMusic({ action: state.playing ? 'pause' : 'play' }));
  $('#playlist-play').addEventListener('click', () => playPlaylist().catch((error) => toast(error.message, true)));
  $('#playlist-next').addEventListener('click', () => nextPlaylistTrack().catch((error) => toast(error.message, true)));
  $('#playlist-refresh').addEventListener('click', () => loadPlaylist().catch((error) => toast(error.message, true)));
  $('#volume-slider').addEventListener('input', (event) => setVolume(event.target.value));
  $('#browser-open-form').addEventListener('submit', (event) => {
    event.preventDefault(); withFormBusy(event.currentTarget, async () => {
      await browserAction({ action: 'open', url: $('#browser-url').value.trim() });
    });
  });
  $('#browser-action').addEventListener('change', updateBrowserActionFields);
  $('#browser-action-form').addEventListener('submit', (event) => {
    event.preventDefault(); withFormBusy(event.currentTarget, () => browserAction({
      action: $('#browser-action').value, target: $('#browser-target').value.trim(),
      value: $('#browser-value').value, direction: $('#browser-direction').value,
    }));
  });
  $('#browser-back').addEventListener('click', () => browserAction({ action: 'back' }).catch((error) => toast(error.message, true)));
  $('#diary-form').addEventListener('submit', (event) => {
    event.preventDefault(); withFormBusy(event.currentTarget, async () => {
      const content = $('#diary-content').value.trim();
      if (!content) throw new Error('Жазба мәтінін енгізіңіз.');
      await post('/api/diary', { content }); $('#diary-content').value = ''; await loadDiary(); toast('Ойыңыз күнделікке сақталды.');
    });
  });
  $('#reminder-form').addEventListener('submit', (event) => {
    event.preventDefault(); withFormBusy(event.currentTarget, async () => {
      const title = $('#reminder-title').value.trim();
      const minutes = Number($('#reminder-delay').value);
      if (!title || !Number.isFinite(minutes) || minutes < 1 || minutes > 43200) throw new Error('Еске салғыш атауын және 1–43200 аралығындағы минут санын енгізіңіз.');
      await post('/api/reminders', { title, delay_seconds: Math.round(minutes * 60) });
      $('#reminder-title').value = ''; await loadReminders(); toast('Еске салғыш қосылды. Терезені ашық қалдырыңыз.');
    });
  });

  const today = new Date();
  $('#today').dateTime = today.toISOString();
  $('#today').textContent = dateLabel(today, false);
  showView(window.location.hash.slice(1) || 'home', false);
  updateSpeechToggle(); updateBrowserActionFields(); setVolume(state.volume); configureVoice();
  state.settingsPromise = loadSettings();
  state.settingsPromise.catch(() => { $('#settings-status').textContent = 'Баптаулар жүктелмеді'; });
  checkHealth();
  loadPlaylist().catch(() => { $('#playlist-notice').textContent = 'Сақталған плейлистті жүктеу мүмкін болмады. Жаңарту батырмасын басып көріңіз.'; });
  setInterval(sampleListening, 1000);
  Promise.allSettled([loadDiary(), loadReminders()]).then((results) => {
    if (results.some((result) => result.status === 'rejected')) toast('Жазбаларды жүктеу мүмкін болмады. Бағдарламамен байланысты тексеріңіз.', true);
  });
  setInterval(() => {
    loadReminders().catch(() => { /* Health state supplies connection feedback. */ });
  }, 15000);
  setInterval(checkHealth, 60000);
  window.addEventListener('beforeunload', () => {
    state.closing = true; clearTimeout(state.wakePollTimer);
    if (state.listenSession) recordListen(state.listenSession, true, true);
    if (state.recognition && state.listening) state.recognition.abort();
    stopSpeech();
    $('#llm-api-key').value = ''; $('#elevenlabs-api-key').value = '';
  });
})();
