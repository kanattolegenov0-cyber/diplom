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
    settings: { llm_enabled: true, llm_model: 'gpt-4.1-mini', llm_configured: false, tts_provider: 'edge', tts_voice: 'kk-KZ-DauletNeural', elevenlabs_configured: false, elevenlabs_voice_id: '', elevenlabs_model: 'eleven_v3' },
    speechGeneration: 0, speechAbort: null, speechAudio: null, speechUrl: null,
    lastSpeechText: '', lastSpeechLanguage: 'kk-KZ',
  };
  const viewNames = { home: 'Басты бет', music: 'Музыка', browser: 'Браузер', diary: 'Күнделік', reminders: 'Еске салғыштар', settings: 'Баптаулар' };
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
    speechFeedback(description);
  }
  async function playSpeechAudio(audio, generation) {
    try {
      await audio.play();
      if (generation === state.speechGeneration) speechFeedback('Жауап дауысталып жатыр.', { stop: true });
    } catch (error) {
      if (generation !== state.speechGeneration) return;
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
    if (state.settings.tts_provider === 'system') {
      const matchingVoice = 'speechSynthesis' in window && speechSynthesis.getVoices().find((voice) =>
        voice.lang.toLowerCase().replace('_', '-').split('-')[0] === language.toLowerCase().split('-')[0]);
      if (!matchingVoice) {
        speechFeedback('Компьютерде осы тілдің дауысы жоқ. Баптаулардан Edge дауысын таңдаңыз немесе жауапты мәтіннен оқыңыз.', { retry: true, error: true });
        return;
      }
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.voice = matchingVoice; utterance.lang = matchingVoice.lang; utterance.volume = state.volume / 100;
      utterance.onstart = () => { if (generation === state.speechGeneration) speechFeedback('Жауап дауысталып жатыр.', { stop: true }); };
      utterance.onend = () => { if (generation === state.speechGeneration) speechFeedback('Жауап дауысталды.'); };
      utterance.onerror = () => { if (generation === state.speechGeneration) speechFeedback('Компьютер дауысы іске қосылмады. Жауап мәтінде сақталды.', { retry: true, error: true }); };
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
        releaseSpeechAudio(); speechFeedback('Жауап дауысталды.');
      };
      audio.onerror = () => {
        if (generation !== state.speechGeneration) return;
        releaseSpeechAudio(); speechFeedback('Дыбыс файлы ойнатылмады. Жауап мәтінде сақталды.', { retry: true, error: true });
      };
      await playSpeechAudio(audio, generation);
    } catch (error) {
      if (generation !== state.speechGeneration || !state.speech) return;
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
    if (state.nativeListening) return;
    const native = Boolean(state.health?.desktop_mode && state.health?.native_voice_available);
    $('#mic-button').disabled = !native && !state.recognition;
    if (native) {
      $('#voice-notice').textContent = 'Микрофонды басып, «Айтыңыз» деген белгі шыққаннан кейін сөйлеңіз. Дауыс тану үшін интернет керек; фондық тыңдау жоқ.';
    } else if (state.recognition) {
      $('#voice-notice').textContent = 'Дауыс тану Chrome/Edge және HTTPS/localhost-та қолжетімді. Тек микрофонды басқан кезде тыңдаймын.';
    } else {
      $('#voice-notice').textContent = 'Бұл ортада дауыс тану қолжетімсіз. Пәрменді жазыңыз немесе Chrome/Edge арқылы localhost-та ашыңыз.';
    }
  }
  async function listen() {
    if (state.commandBusy || state.nativeListening) return;
    stopSpeech();
    if (state.health?.desktop_mode && state.health?.native_voice_available) {
      state.nativeListening = true;
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
        const listenRequest = post('/api/voice/listen', { language: state.language }, { timeout: 45000 });
        pollVoiceStatus();
        const result = await listenRequest;
        stopPolling();
        setListening(false);
        const text = String(result.text || '').trim();
        if (text) { $('#command-input').value = text; await handleCommand(text); }
        else toast('Пәрмен анық естілмеді. Қайта айтыңыз немесе мәтінмен жазыңыз.', true);
      } catch (error) { toast(error.message, true); }
      finally { stopPolling(); state.nativeListening = false; setListening(false); configureVoice(); }
      return;
    }
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
  }

  function renderEngineStatus() {
    const enabled = Boolean(state.settings.llm_enabled && state.settings.llm_configured);
    const badge = $('#engine-status');
    badge.classList.toggle('ai-ready', enabled);
    badge.textContent = enabled ? `${state.lastEngine === 'openai' ? 'GPT' : 'GPT дайын'} · ${state.settings.llm_model}` : 'Жергілікті пәрмендер';
    badge.title = enabled ? 'GPT қосылған. Нақты пәрмендер жергілікті де орындала алады.' : 'GPT үшін Баптауларға OpenAI API кілтін қосыңыз.';
  }
  function updateProviderFields() {
    const provider = $('#tts-provider').value;
    $('#edge-voice-field').hidden = provider !== 'edge';
    $('#elevenlabs-fields').hidden = provider !== 'elevenlabs';
    $('#system-voice-note').hidden = provider !== 'system';
  }
  function acceptSettings(settings) {
    const fields = ['llm_enabled', 'llm_model', 'llm_configured', 'tts_provider', 'tts_voice', 'elevenlabs_configured', 'elevenlabs_voice_id', 'elevenlabs_model'];
    for (const field of fields) if (Object.hasOwn(settings, field)) state.settings[field] = settings[field];
    $('#llm-enabled').checked = Boolean(state.settings.llm_enabled);
    $('#llm-model').value = state.settings.llm_model || 'gpt-4.1-mini';
    $('#tts-provider').value = state.settings.tts_provider || 'edge';
    $('#tts-voice').value = state.settings.tts_voice || 'kk-KZ-DauletNeural';
    $('#elevenlabs-voice-id').value = state.settings.elevenlabs_voice_id || '';
    $('#elevenlabs-model').value = state.settings.elevenlabs_model || 'eleven_v3';
    $('#llm-key-status').textContent = state.settings.llm_configured ? 'кілт сақталған' : 'қосылмаған';
    $('#elevenlabs-key-status').textContent = state.settings.elevenlabs_configured ? 'кілт сақталған' : 'қосылмаған';
    $('#clear-llm-key').checked = false;
    $('#clear-elevenlabs-key').checked = false;
    $('#clear-llm-key').disabled = !state.settings.llm_configured;
    $('#clear-elevenlabs-key').disabled = !state.settings.elevenlabs_configured;
    $('#settings-status').textContent = state.settings.llm_enabled && state.settings.llm_configured ? 'GPT қосылған' : 'Негізгі пәрмендер дайын';
    updateProviderFields(); renderEngineStatus();
  }
  async function loadSettings() { acceptSettings(await api('/api/settings')); }
  async function saveSettings() {
    const result = $('#settings-result');
    result.classList.remove('error'); result.textContent = 'Баптаулар сақталуда…';
    const body = {
      llm_enabled: $('#llm-enabled').checked, llm_model: $('#llm-model').value.trim(),
      tts_provider: $('#tts-provider').value, tts_voice: $('#tts-voice').value,
      elevenlabs_voice_id: $('#elevenlabs-voice-id').value.trim(),
      elevenlabs_model: $('#elevenlabs-model').value.trim() || 'eleven_v3',
      llm_api_key: $('#llm-api-key').value.trim(), elevenlabs_api_key: $('#elevenlabs-api-key').value.trim(),
      clear_llm_api_key: $('#clear-llm-key').checked, clear_elevenlabs_api_key: $('#clear-elevenlabs-key').checked,
    };
    try {
      if ((body.clear_llm_api_key && body.llm_api_key) || (body.clear_elevenlabs_api_key && body.elevenlabs_api_key)) throw new Error('Кілтті жою немесе жаңарту әрекетінің бірін таңдаңыз.');
      const settings = await post('/api/settings', body);
      stopSpeech(); acceptSettings(settings);
      $('#llm-api-key').value = ''; $('#elevenlabs-api-key').value = '';
      result.textContent = 'Баптаулар сақталды. Таңдалған дауысты тексеріп көріңіз.';
    } catch (error) { result.classList.add('error'); result.textContent = error.message; throw error; }
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
      button.addEventListener('click', () => selectTrack(track).catch((error) => toast(error.message, true)));
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
      await selectTrack(validTracks[0]);
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
  async function selectTrack(track) {
    state.currentTrack = track;
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
              onStateChange: (event) => {
                updatePlayback(event.data === YT.PlayerState.PLAYING);
                if (event.data === YT.PlayerState.PLAYING) $('#music-notice').textContent = 'Ойнап жатыр. Дыбыс деңгейін өзгерте аласыз.';
                if (event.data === YT.PlayerState.ENDED) $('#music-notice').textContent = 'Әуен аяқталды. Басқа әнді таңдаңыз.';
              },
              onError: () => {
                const errorMessage = 'Бұл бейнені осы ойнатқышта ашу мүмкін болмады. YouTube-та ашу сілтемесін пайдаланыңыз.';
                updatePlayback(false); $('#music-notice').textContent = errorMessage;
                if (!state.playerReady) { clearTimeout(timer); reject(new Error(errorMessage)); }
              },
              onAutoplayBlocked: () => { $('#music-notice').textContent = 'Ойнату батырмасын басыңыз: автоматты ойнатуға рұқсат берілмеді.'; },
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
      case 'music_control':
        showView('music');
        if (inPlan && payload.action !== 'volume' && !state.playerReady) throw new Error('Алдымен әнді таңдап, ойнатқыштың дайын болғанын күтіңіз.');
        return controlMusic(payload);
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
    const allowed = ['music_search', 'music_control', 'browser_open', 'browser_action', 'diary_add', 'diary_list', 'reminder_add', 'reminders_list', 'time'];
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
    try {
      const result = await post('/api/command', { text, volume: state.volume, language: state.language, history });
      state.lastEngine = result.engine || null; renderEngineStatus();
      const reply = result.intent === 'plan' ? await executePlan(result) : await executeIntent(result.intent, result.payload || {}, result.message || 'Пәрмен қабылданды.');
      message(reply);
    } catch (error) { message(error.message, 'assistant', true); }
    finally { state.commandBusy = false; send.disabled = false; setAssistantState(state.listening ? 'Тыңдап тұрмын…' : 'Пәрменіңізді күтіп тұрмын'); }
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
  $('#play-toggle').addEventListener('click', () => controlMusic({ action: state.playing ? 'pause' : 'play' }));
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
  Promise.allSettled([loadDiary(), loadReminders()]).then((results) => {
    if (results.some((result) => result.status === 'rejected')) toast('Жазбаларды жүктеу мүмкін болмады. Бағдарламамен байланысты тексеріңіз.', true);
  });
  setInterval(() => {
    loadReminders().catch(() => { /* Health state supplies connection feedback. */ });
  }, 15000);
  setInterval(checkHealth, 60000);
  window.addEventListener('beforeunload', () => {
    if (state.recognition && state.listening) state.recognition.abort();
    stopSpeech();
    $('#llm-api-key').value = ''; $('#elevenlabs-api-key').value = '';
  });
})();
