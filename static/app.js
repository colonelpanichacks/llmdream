let ws;
let autoChatActive = true;
let streamingMessages = {}; // model_id -> element
let currentSessionId = null;
let sessionMemories = {}; // model_id -> memory string (per-session)

// Markdown setup
marked.setOptions({
    highlight: function(code, lang) {
        if (lang && hljs.getLanguage(lang)) {
            return hljs.highlight(code, { language: lang }).value;
        }
        return hljs.highlightAuto(code).value;
    },
    breaks: true,
});

let reconnectTimer = null;

function connect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${location.host}/ws`);

    ws.onopen = () => {
        console.log('Connected to chatroom');
    };

    ws.onclose = () => {
        console.log('Disconnected, reconnecting in 2s...');
        reconnectTimer = setTimeout(connect, 2000);
    };

    ws.onerror = () => {
        try { ws.close(); } catch(e) {}
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        handleMessage(data);
    };
}

// Reconnect when mobile browser comes back from background
document.addEventListener('visibilitychange', () => {
    if (!document.hidden && (!ws || ws.readyState > 1)) {
        connect();
    }
});

function handleMessage(data) {
    switch (data.type) {
        case 'init':
            populateModelSelect(data.available_models);
            loadActiveModels(data.active_models);
            document.getElementById('user-profile').value = data.user_profile || '';
            loadHistory(data.history);
            if (data.auto_chat_active) {
                autoChatActive = true;
                updateAutoChatButton();
            }
            // Sessions
            if (data.sessions) {
                currentSessionId = data.current_session_id;
                renderSessions(data.sessions, data.current_session_id);
            }
            // Model memory (per-session)
            sessionMemories = data.model_memories || {};
            renderModelMemory();
            // SD settings
            if (data.sd_available) {
                loadSdSettings(data.sd_settings);
            }
            // Stage interval
            if (data.stage_interval_minutes) {
                const slider = document.getElementById('stage-interval');
                if (slider) {
                    slider.value = data.stage_interval_minutes;
                    const label = document.getElementById('stage-interval-val');
                    if (label) {
                        const v = data.stage_interval_minutes;
                        label.textContent = v >= 60 ? `${(v/60).toFixed(1)} hr` : `${v} min`;
                    }
                    updateSliderFill(slider);
                }
            }
            break;

        case 'available_models':
            populateModelSelect(data.models);
            break;

        case 'message':
            const msgColor = data.role === 'user' ? '#00b4d8' : (data.color || activeModels[data.role]?.color || '#888');
            addMessage(data.role === 'user' ? 'User' : data.name, data.content, msgColor);
            break;

        case 'thinking':
            showThinking(data.model_id, data.name, data.color);
            break;

        case 'start':
            removeThinking(data.model_id);
            startStreaming(data.model_id, data.name, data.color);
            break;

        case 'token':
            appendToken(data.model_id, data.token);
            break;

        case 'end':
            endStreaming(data.model_id);
            break;

        case 'image_inline_start':
            showInlineImageSpinner(data.model_id, data.prompt);
            break;

        case 'image_inline_ready':
            replaceInlineImageSpinner(data.model_id, data.url);
            break;

        case 'image_inline_failed':
            failInlineImageSpinner(data.model_id);
            break;

        case 'queued':
            showQueuedIndicator();
            break;

        case 'error':
            addSystemMessage(data.message);
            break;

        case 'system':
            addSystemMessage(data.message);
            break;

        case 'model_added':
            if (data.config) {
                activeModels[data.model_id] = data.config;
            }
            renderActiveModels();
            renderModelMemory();
            break;

        case 'model_removed':
            delete activeModels[data.model_id];
            renderActiveModels();
            renderModelMemory();
            break;

        case 'model_updated':
            if (data.config) {
                activeModels[data.model_id] = data.config;
            }
            break;

        case 'sd_settings':
            loadSdSettings(data.sd_settings);
            break;

        case 'auto_chat_status':
            autoChatActive = data.active;
            updateAutoChatButton();
            if (!data.active) {
                hideStageIndicator();
                if (_autoKillTimer) { clearTimeout(_autoKillTimer); _autoKillTimer = null; }
            } else {
                startAutoKillTimer();
            }
            break;

        case 'stage_update':
            showStageIndicator(data.current, data.total, data.text);
            break;

        case 'stages_schedule':
            showStagesSchedule(data.stages, data.interval_minutes, data.current);
            break;

        case 'killed':
            autoChatActive = false;
            updateAutoChatButton();
            for (const mid of Object.keys(streamingMessages)) {
                endStreaming(mid);
            }
            for (const mid of Object.keys(thinkingElements)) {
                removeThinking(mid);
            }
            if (_stagesTimerInterval) { clearInterval(_stagesTimerInterval); _stagesTimerInterval = null; }
            const pinnedSched = document.getElementById('stages-schedule-pinned');
            if (pinnedSched) { pinnedSched.style.display = 'none'; pinnedSched.innerHTML = ''; }
            if (data.message) addSystemMessage(data.message);
            break;

        case 'history_cleared':
            document.getElementById('messages').innerHTML = '';
            addSystemMessage('History cleared.');
            break;

        // Session management
        case 'sessions_list':
            currentSessionId = data.current_session_id;
            renderSessions(data.sessions, data.current_session_id);
            break;

        case 'session_loaded':
            currentSessionId = data.session_id;
            sessionMemories = data.model_memories || {};
            document.getElementById('messages').innerHTML = '';
            if (data.history && data.history.length) {
                loadHistory(data.history);
            }
            renderModelMemory();
            break;
    }
}

// ═══ Sessions ═══

function renderSessions(sessions, currentId) {
    const container = document.getElementById('sessions-list');
    container.innerHTML = '';

    if (!sessions || sessions.length === 0) {
        container.innerHTML = '<div class="sessions-empty">No sessions yet</div>';
        return;
    }

    for (const s of sessions) {
        const item = document.createElement('div');
        item.className = 'session-item' + (s.id === currentId ? ' active' : '');
        item.onclick = (e) => {
            if (e.target.classList.contains('session-delete') || e.target.closest('.session-delete')) return;
            if (e.target.classList.contains('session-gear') || e.target.closest('.session-gear')) return;
            if (e.target.closest('.session-menu')) return;
            if (e.target.hasAttribute('contenteditable')) return;
            switchSession(s.id);
        };

        // Format date
        let dateStr = '';
        if (s.updated_at) {
            const d = new Date(s.updated_at);
            const now = new Date();
            const diffMs = now - d;
            const diffDays = Math.floor(diffMs / 86400000);
            if (diffDays === 0) {
                dateStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            } else if (diffDays === 1) {
                dateStr = 'Yesterday';
            } else if (diffDays < 7) {
                dateStr = d.toLocaleDateString([], { weekday: 'short' });
            } else {
                dateStr = d.toLocaleDateString([], { month: 'short', day: 'numeric' });
            }
        }

        item.innerHTML = `
            <div class="session-info">
                <div class="session-title" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</div>
                <div class="session-meta">
                    <span>${dateStr}</span>
                    ${s.message_count ? `<span class="session-msg-count">${s.message_count} msgs</span>` : ''}
                </div>
            </div>
            <div class="session-actions">
                <button class="session-gear" onclick="event.stopPropagation();toggleSessionMenu('${s.id}', this)" title="Options">⚙</button>
                <button class="session-delete" onclick="event.stopPropagation();deleteSession('${s.id}')" title="Delete">&times;</button>
            </div>
            <div class="session-menu" id="session-menu-${s.id}">
                <button onclick="event.stopPropagation();exportSessionStorybook('${s.id}')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg> Storybook</button>
                <button onclick="event.stopPropagation();exportSessionFlipbook('${s.id}')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="2" width="20" height="20" rx="2.18" ry="2.18"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="2" y1="7" x2="7" y2="7"/><line x1="2" y1="17" x2="7" y2="17"/><line x1="17" y1="7" x2="22" y2="7"/><line x1="17" y1="17" x2="22" y2="17"/></svg> Flip Book</button>
                <button onclick="event.stopPropagation();exportSessionPrompts('${s.id}')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg> Prompts</button>
            </div>
        `;

        // Double-click title to rename
        const titleEl = item.querySelector('.session-title');
        titleEl.addEventListener('dblclick', (e) => {
            e.stopPropagation();
            titleEl.contentEditable = 'true';
            titleEl.focus();
            // Select all text
            const range = document.createRange();
            range.selectNodeContents(titleEl);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
        });

        titleEl.addEventListener('blur', () => {
            titleEl.contentEditable = 'false';
            const newTitle = titleEl.textContent.trim();
            if (newTitle && newTitle !== s.title) {
                renameSession(s.id, newTitle);
            }
        });

        titleEl.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                titleEl.blur();
            }
            if (e.key === 'Escape') {
                titleEl.textContent = s.title;
                titleEl.blur();
            }
        });

        container.appendChild(item);
    }
}

function createSession() {
    safeSend({ action: 'create_session' });
}

function switchSession(sessionId) {
    if (sessionId === currentSessionId) return;
    safeSend({ action: 'switch_session', session_id: sessionId });
}

function deleteSession(sessionId) {
    if (!confirm('Delete this session?')) return;
    safeSend({ action: 'delete_session', session_id: sessionId });
}

function renameSession(sessionId, newTitle) {
    safeSend({ action: 'rename_session', session_id: sessionId, title: newTitle });
}

// ═══ Settings Modal ═══

function openSettings() {
    const overlay = document.getElementById('settings-overlay-modal');
    overlay.classList.add('open');
    // Refresh model memory when opening
    renderModelMemory();
    // Init all slider fills in settings
    const modal = overlay.querySelector('.settings-modal');
    if (modal) initAllSliderFills(modal);
}

function closeSettings() {
    const overlay = document.getElementById('settings-overlay-modal');
    overlay.classList.remove('open');
}

// ═══ Model Memory ═══

function renderModelMemory() {
    const container = document.getElementById('model-memory-list');
    if (!container) return;
    container.innerHTML = '';

    if (Object.keys(activeModels).length === 0) {
        container.innerHTML = '<div class="sessions-empty">No active models</div>';
        return;
    }

    for (const [mid, cfg] of Object.entries(activeModels)) {
        const item = document.createElement('div');
        item.className = 'memory-item';
        const memory = sessionMemories[mid] || '';
        item.innerHTML = `
            <div class="memory-item-header">
                <span class="dot" style="background:${cfg.color}"></span>
                <span class="name">${escapeHtml(cfg.display_name)}</span>
            </div>
            <textarea placeholder="Things this model should always remember..." rows="3">${escapeHtml(memory)}</textarea>
            <button class="btn-sm" onclick="saveModelMemory('${mid}', this)">Save</button>
        `;
        container.appendChild(item);
    }
}

function saveModelMemory(modelId, btn) {
    const item = btn.closest('.memory-item');
    const textarea = item.querySelector('textarea');
    const memory = textarea.value;
    safeSend({ action: 'update_model_memory', model_id: modelId, memory: memory });
    // Visual feedback
    const orig = btn.textContent;
    btn.textContent = 'Saved!';
    btn.style.color = 'var(--success)';
    setTimeout(() => { btn.textContent = orig; btn.style.color = ''; }, 1500);
}

// ═══ Model Select ═══

function populateModelSelect(models) {
    const sel = document.getElementById('model-select');
    sel.innerHTML = '<option value="">Select a model...</option>';
    for (const m of models) {
        const opt = document.createElement('option');
        opt.value = m.name;
        const sizeMB = (m.size / 1e9).toFixed(1);
        opt.textContent = `${m.name} (${sizeMB}GB)`;
        sel.appendChild(opt);
    }
}

function refreshModels() {
    const host = document.getElementById('ollama-host').value.trim() || 'http://localhost:11434';
    safeSend({ action: 'refresh_models', host: host });
}

// ═══ Slider Fill ═══

function updateSliderFill(input) {
    const pct = ((input.value - input.min) / (input.max - input.min)) * 100;
    const accentColor = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#00ffc8';
    const borderColor = getComputedStyle(document.documentElement).getPropertyValue('--border').trim() || '#333';
    input.style.setProperty('background', `linear-gradient(to right, ${accentColor} 0%, ${accentColor} ${pct}%, ${borderColor} ${pct}%, ${borderColor} 100%)`, 'important');
}

function initAllSliderFills(container) {
    container.querySelectorAll('input[type="range"]').forEach(s => {
        updateSliderFill(s);
        // Ensure realtime updates on all platforms (mobile especially)
        if (!s._fillWired) {
            s.addEventListener('input', () => updateSliderFill(s));
            s.addEventListener('change', () => updateSliderFill(s));
            s._fillWired = true;
        }
    });
}

// ═══ Active Models ═══

let activeModels = {};

function loadActiveModels(models) {
    activeModels = models || {};
    renderActiveModels();
}

function renderActiveModels() {
    const container = document.getElementById('active-models-list');
    container.innerHTML = '';
    for (const [mid, cfg] of Object.entries(activeModels)) {
        const temp = cfg.temperature ?? 0.7;
        const topP = cfg.top_p ?? 0.9;
        const topK = cfg.top_k ?? 40;
        const repPen = cfg.repeat_penalty ?? 1.1;
        const host = cfg.ollama_host || 'http://localhost:11434';
        const card = document.createElement('div');
        card.className = 'model-card';
        card.style.flexWrap = 'wrap';
        card.innerHTML = `
            <span class="dot" style="background:${cfg.color}"></span>
            <div style="flex:1;min-width:0">
                <div class="name">${escapeHtml(cfg.display_name)}</div>
                <div class="model-id">${escapeHtml(mid)}</div>
            </div>
            <button class="model-kill-btn" onclick="confirmRemoveModel('${mid}')" title="Remove model">&times;</button>
            <button class="settings-toggle" onclick="toggleModelSettings('${mid}')" title="Expand settings">&#9662;</button>
            <div class="model-settings" id="settings-${CSS.escape(mid)}">
                <div class="param-group">
                    <label>Display Name</label>
                    <input type="text" value="${escapeHtml(cfg.display_name)}" placeholder="Model name..."
                        onchange="renameModel('${mid}', this.value)">
                </div>
                <div class="param-group">
                    <label>Ollama Host</label>
                    <input type="text" value="${escapeHtml(host)}"
                        onchange="updateModelParam('${mid}','ollama_host',this.value)">
                </div>
                <div class="param-group">
                    <label>Color</label>
                    <input type="color" value="${cfg.color || '#6c9f5e'}" class="model-color-picker"
                        oninput="updateModelColor('${mid}', this.value, this.closest('.model-card'))">
                </div>
                <div class="param-group" title="Controls randomness. Higher = more creative. Lower = more focused. 0 = deterministic.">
                    <label>Temperature <span id="live-temp-${CSS.escape(mid)}">${temp}</span></label>
                    <input type="range" min="0" max="2" step="0.05" value="${temp}"
                        oninput="updateModelParam('${mid}','temperature',parseFloat(this.value));document.getElementById('live-temp-${CSS.escape(mid)}').textContent=this.value;updateSliderFill(this)">
                </div>
                <div class="param-group" title="Nucleus sampling. Lower = more focused. Higher = considers more options.">
                    <label>Top P <span id="live-topp-${CSS.escape(mid)}">${topP}</span></label>
                    <input type="range" min="0" max="1" step="0.05" value="${topP}"
                        oninput="updateModelParam('${mid}','top_p',parseFloat(this.value));document.getElementById('live-topp-${CSS.escape(mid)}').textContent=this.value;updateSliderFill(this)">
                </div>
                <div class="param-group" title="Limits to top K tokens. Lower = predictable. Higher = diverse.">
                    <label>Top K <span id="live-topk-${CSS.escape(mid)}">${topK}</span></label>
                    <input type="range" min="1" max="200" step="1" value="${topK}"
                        oninput="updateModelParam('${mid}','top_k',parseInt(this.value));document.getElementById('live-topk-${CSS.escape(mid)}').textContent=this.value;updateSliderFill(this)">
                </div>
                <div class="param-group" title="Penalizes repetition. Higher = less repetition. 1.0 = no penalty.">
                    <label>Repeat Penalty <span id="live-rep-${CSS.escape(mid)}">${repPen}</span></label>
                    <input type="range" min="1.0" max="2.0" step="0.05" value="${repPen}"
                        oninput="updateModelParam('${mid}','repeat_penalty',parseFloat(this.value));document.getElementById('live-rep-${CSS.escape(mid)}').textContent=this.value;updateSliderFill(this)">
                </div>
                <div class="model-remove-zone">
                    <button class="btn-sm btn-danger model-remove-btn" onclick="confirmRemoveModel('${mid}')">Remove Model</button>
                </div>
            </div>
        `;
        container.appendChild(card);
        initAllSliderFills(card);
    }
    updateModelCount();
}

function toggleModelSettings(modelId) {
    const el = document.getElementById('settings-' + CSS.escape(modelId));
    if (el) {
        el.classList.toggle('open');
        // Rotate chevron
        const card = el.closest('.model-card');
        const btn = card?.querySelector('.settings-toggle');
        if (btn) btn.innerHTML = el.classList.contains('open') ? '&#9652;' : '&#9662;';
    }
}

function confirmRemoveModel(modelId) {
    const name = activeModels[modelId]?.display_name || modelId;
    if (confirm(`Remove "${name}" from the chatroom?`)) {
        removeModel(modelId);
    }
}

function renameModel(modelId, newName) {
    const name = newName.trim();
    if (!name) return;
    if (activeModels[modelId]) {
        activeModels[modelId].display_name = name;
    }
    safeSend({ action: 'update_model', model_id: modelId, display_name: name });
    // Update the name in the model card immediately
    renderActiveModels();
}

function updateModelColor(modelId, color, card) {
    if (activeModels[modelId]) {
        activeModels[modelId].color = color;
    }
    const dot = card.querySelector('.dot');
    if (dot) dot.style.background = color;
    clearTimeout(_updateTimers['color_' + modelId]);
    _updateTimers['color_' + modelId] = setTimeout(() => {
        safeSend({ action: 'update_model', model_id: modelId, color: color });
    }, 300);
}

let _updateTimers = {};
function updateModelParam(modelId, param, value) {
    if (activeModels[modelId]) {
        activeModels[modelId][param] = value;
    }
    const key = modelId + param;
    clearTimeout(_updateTimers[key]);
    _updateTimers[key] = setTimeout(() => {
        const msg = { action: 'update_model', model_id: modelId };
        msg[param] = value;
        safeSend(msg);
    }, 300);
}

function safeSend(data) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        addSystemMessage('Not connected — reconnecting...');
        connect();
        setTimeout(() => {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify(data));
            } else {
                addSystemMessage('Still disconnected. Try again in a moment.');
            }
        }, 1500);
        return false;
    }
    try {
        ws.send(JSON.stringify(data));
        return true;
    } catch (e) {
        addSystemMessage('Send failed — reconnecting...');
        connect();
        return false;
    }
}

function addModel() {
    const sel = document.getElementById('model-select');
    const modelId = sel.value;
    if (!modelId) {
        addSystemMessage('Select a model first.');
        return;
    }


    const displayName = document.getElementById('model-display-name').value || modelId.split(':')[0];
    const color = document.getElementById('model-color').value;
    const systemPrompt = document.getElementById('model-system-prompt').value;
    const temperature = parseFloat(document.getElementById('model-temperature').value);
    const topP = parseFloat(document.getElementById('model-top-p').value);
    const topK = parseInt(document.getElementById('model-top-k').value);
    const repeatPenalty = parseFloat(document.getElementById('model-repeat-penalty').value);
    const ollamaHost = document.getElementById('ollama-host').value.trim() || 'http://localhost:11434';

    const payload = {
        action: 'add_model',
        model_id: modelId,
        display_name: displayName,
        system_prompt: systemPrompt,
        color: color,
        temperature: temperature,
        top_p: topP,
        top_k: topK,
        repeat_penalty: repeatPenalty,
        ollama_host: ollamaHost,
    };

    if (!safeSend(payload)) return;

    activeModels[modelId] = {
        display_name: displayName, system_prompt: systemPrompt, color: color,
        temperature, top_p: topP, top_k: topK, repeat_penalty: repeatPenalty,
        ollama_host: ollamaHost, memory: '',
    };
    renderActiveModels();
    updateModelCount();

    // Reset form
    document.getElementById('model-display-name').value = '';
    document.getElementById('model-system-prompt').value = '';
    document.getElementById('model-temperature').value = '0.7';
    document.getElementById('temp-val').textContent = '0.7';
    document.getElementById('model-top-p').value = '0.9';
    document.getElementById('topp-val').textContent = '0.9';
    document.getElementById('model-top-k').value = '40';
    document.getElementById('topk-val').textContent = '40';
    document.getElementById('model-repeat-penalty').value = '1.1';
    document.getElementById('rep-val').textContent = '1.1';
    initAllSliderFills(document.getElementById('add-model-section'));

    addSystemMessage(`Added ${displayName} to chat.`);
}

function updateModelCount() {
    const count = Object.keys(activeModels).length;
    const el = document.getElementById('model-count-indicator');
    if (el) el.textContent = `${count} model${count !== 1 ? 's' : ''} active`;
}

function removeModel(modelId) {
    safeSend({ action: 'remove_model', model_id: modelId });
    delete activeModels[modelId];
    renderActiveModels();
    if (Object.keys(activeModels).length < 2 && autoChatActive) {
        safeSend({ action: 'auto_chat_stop' });
    }
}

// ═══ Messages ═══

function addMessage(name, content, color) {
    const container = document.getElementById('messages');
    const msg = createMessageElement(name, content, color);
    container.appendChild(msg);
    scrollToBottom();
}

function createMessageElement(name, content, color, isStreaming = false) {
    const msg = document.createElement('div');
    msg.className = 'message';

    const initial = name.charAt(0).toUpperCase();
    const rendered = isStreaming ? '' : renderMarkdown(content);

    msg.innerHTML = `
        <div class="avatar" style="background:${color}">${initial}</div>
        <div class="body">
            <div class="header">
                <span class="sender" style="color:${color}">${escapeHtml(name)}</span>
                <span class="timestamp">${new Date().toLocaleTimeString()}</span>
            </div>
            <div class="content">${rendered}</div>
        </div>
    `;
    return msg;
}

// ═══ Inline Image Generation (appended to streaming message) ═══

function showInlineImageSpinner(modelId, prompt) {
    const stream = streamingMessages[modelId];
    if (!stream) return;
    const contentEl = stream.element.querySelector('.content');
    // Remove streaming indicator, append spinner
    const indicator = contentEl.querySelector('.streaming-indicator');
    if (indicator) indicator.remove();
    const placeholder = document.createElement('div');
    placeholder.className = 'image-placeholder';
    placeholder.innerHTML = `
        <div class="image-spinner"></div>
        <div class="image-gen-prompt">
            <div class="image-gen-label"><span class="tool-icon">&#9998;</span> generating image</div>
            <span class="prompt-text">${escapeHtml(prompt)}</span>
        </div>
    `;
    contentEl.appendChild(placeholder);
    scrollToBottom();
}

function replaceInlineImageSpinner(modelId, url) {
    const stream = streamingMessages[modelId];
    if (!stream) return;
    const contentEl = stream.element.querySelector('.content');
    const placeholder = contentEl.querySelector('.image-placeholder');
    if (placeholder) {
        const img = document.createElement('img');
        img.src = url;
        img.alt = 'Generated image';
        img.className = 'inline-image';
        img.onload = () => forceScrollToBottom();
        img.onerror = function() {
            this.dataset.retries = (this.dataset.retries || 0);
            if (this.dataset.retries < 3) {
                this.dataset.retries++;
                setTimeout(() => this.src = this.src, 1000);
            } else {
                this.style.opacity = '0.3';
            }
        };
        placeholder.replaceWith(img);
    }
    // Track image URL — don't put in rawText (endStreaming strips ![])
    if (!stream.inlineImages) stream.inlineImages = [];
    stream.inlineImages.push(url);
    scrollToBottom();
}

function failInlineImageSpinner(modelId) {
    const stream = streamingMessages[modelId];
    if (!stream) return;
    const contentEl = stream.element.querySelector('.content');
    const placeholder = contentEl.querySelector('.image-placeholder');
    if (placeholder) {
        placeholder.innerHTML = '<span class="image-gen-failed">Image generation failed</span>';
    }
}

function showQueuedIndicator() {
    // Remove any previous queued styling
    document.querySelectorAll('.message-queued').forEach(el => el.classList.remove('message-queued'));
    document.querySelectorAll('.queued-badge').forEach(el => el.remove());

    // Find the last user message and mark it as queued
    const messages = document.querySelectorAll('#messages .message');
    for (let i = messages.length - 1; i >= 0; i--) {
        const sender = messages[i].querySelector('.sender');
        if (sender && sender.textContent === 'User') {
            messages[i].classList.add('message-queued');
            const badge = document.createElement('div');
            badge.className = 'queued-badge';
            badge.innerHTML = `
                <span class="queued-dot"></span>
                <span class="queued-text">QUEUED</span>
                <span class="queued-hint">send again to interrupt</span>
            `;
            messages[i].querySelector('.body').appendChild(badge);
            scrollToBottom();
            // Auto-remove after 6 seconds
            setTimeout(() => {
                messages[i]?.classList.remove('message-queued');
                badge.remove();
            }, 6000);
            break;
        }
    }
}

function addSystemMessage(text) {
    const container = document.getElementById('messages');
    const msg = document.createElement('div');
    msg.className = 'message system-msg';
    msg.innerHTML = `<div class="content">${escapeHtml(text)}</div>`;
    container.appendChild(msg);
    scrollToBottom();
}

// ═══ Thinking Indicators ═══

let thinkingElements = {};

function showThinking(modelId, name, color) {
    if (thinkingElements[modelId]) return;
    const container = document.getElementById('messages');
    const msg = createMessageElement(name, '', color, true);
    const contentEl = msg.querySelector('.content');
    contentEl.innerHTML = '<span class="thinking-text">thinking</span><span class="streaming-indicator"></span>';
    msg.classList.add('thinking-msg');
    msg.dataset.modelId = modelId;
    container.appendChild(msg);
    thinkingElements[modelId] = msg;
    forceScrollToBottom();
}

function removeThinking(modelId) {
    if (thinkingElements[modelId]) {
        thinkingElements[modelId].remove();
        delete thinkingElements[modelId];
    }
}

// ═══ Streaming ═══

function startStreaming(modelId, name, color) {
    const container = document.getElementById('messages');
    const msg = createMessageElement(name, '', color, true);
    const contentEl = msg.querySelector('.content');
    contentEl.innerHTML = '<span class="streaming-indicator"></span>';
    container.appendChild(msg);
    streamingMessages[modelId] = { element: msg, content: '', rawText: '' };
    scrollToBottom();
}

function liveClean(text) {
    // Realtime cleanup — hide tool call JSON and fake image refs as they stream in
    return text
        // Strip ALL markdown image references (models invent fake URLs)
        .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
        // Strip complete {"generate_image": "..."} blocks
        .replace(/\{\s*"generate_image"\s*:\s*"[^"]*"\s*\}/gi, '')
        // Strip "Generate_image: prompt text" lines (plain text, no JSON)
        .replace(/^Generate_?image\s*:.*$/gmi, '')
        // Strip complete {"prompt": "..."} blocks and variants
        .replace(/\{[^{}]*"[^"]*(?:prompt|generate)[^"]*"\s*:[^{}]*(?:\{[^{}]*\}[^{}]*)*\}/gi, '')
        // Strip [{"name": "generate_image", ...}] arrays
        .replace(/\[\s*\{\s*"name"\s*:\s*"[^"]*image[^"]*"[\s\S]*?\}\s*\]/gi, '')
        // Strip standalone {"name": "generate_image", "arguments": {...}} objects
        .replace(/\{\s*"name"\s*:\s*"[^"]*image[^"]*"\s*,\s*"arguments"\s*:\s*\{[\s\S]*?\}\s*\}/gi, '')
        // Strip code-fenced blocks with prompt/generate_image
        .replace(/```[^`]*"[^"]*(?:prompt|generate_image)[^"]*"\s*:[^`]*```/gi, '')
        // Strip [TOOL_CALLS] prefix
        .replace(/\[TOOL_CALLS?\]/gi, '')
        // Hide incomplete JSON being typed — opening brace followed by "generate or "prompt
        .replace(/\{\s*"(?:generate_image|prompt|name)[^}]*$/i, '')
        // Strip fake image URLs models write inline
        .replace(/\(?\s*https?:\/\/[^\s)]*generate_image[^\s)]*\s*\)?/gi, '')
        .replace(/\(?\s*https?:\/\/i\.imgur\.com\/[^\s)]*\s*\)?/gi, '')
        // Strip orphaned ``` and json tags
        .replace(/```+\s*```*/g, '')
        .replace(/^\s*```+\s*$/gm, '')
        .replace(/^\s*json\s*$/gmi, '')
        .replace(/\n{3,}/g, '\n\n')
        .trim();
}

function appendToken(modelId, token) {
    const stream = streamingMessages[modelId];
    if (!stream) return;

    stream.rawText += token;
    const displayText = liveClean(stream.rawText);
    const contentEl = stream.element.querySelector('.content');
    if (displayText) {
        contentEl.innerHTML = renderMarkdown(displayText) + '<span class="streaming-indicator"></span>';
    } else {
        contentEl.innerHTML = '<span class="streaming-indicator"></span>';
    }
    scrollToBottom();
}

function endStreaming(modelId) {
    const stream = streamingMessages[modelId];
    if (!stream) return;

    const cleanText = stream.rawText
        .replace(/<generate_image>[\s\S]*?<\/generate_image>/gi, '')
        .replace(/`?\[GENERATE_IMAGE:\s*.+?\]`?/gi, '')
        // Strip JSON tool call arrays: [{"name": "generate_image", ...}]
        .replace(/\[\s*\{\s*"name"\s*:\s*"[^"]*image[^"]*"[\s\S]*?\}\s*\]/gi, '')
        // Strip standalone JSON tool call objects
        .replace(/\{\s*"name"\s*:\s*"[^"]*image[^"]*"\s*,\s*"arguments"\s*:\s*\{[\s\S]*?\}\s*\}/gi, '')
        // Strip {"generate_image": "..."} blocks
        .replace(/\{\s*"generate_image"\s*:\s*"[^"]*"\s*\}/gi, '')
        // Nuclear: strip ANY JSON block containing "prompt" or "generate" key
        .replace(/\{[^{}]*"[^"]*(?:prompt|generate)[^"]*"\s*:[^{}]*(?:\{[^{}]*\}[^{}]*)*\}/gi, '')
        // Strip any code-fenced block containing "prompt" or "generate_image"
        .replace(/```[^`]*"[^"]*(?:prompt|generate_image)[^"]*"\s*:[^`]*```/gi, '')
        .replace(/^\s*Generate\s+image\.?\s*$/gmi, '')
        .replace(/\*\*\s*\*\*/g, '')
        .replace(/```+\s*```*/g, '')
        .replace(/^\s*```+\s*$/gm, '')
        .trim();
    // Strip ALL image markdown from model text — real images come via image_inline_ready
    const textOnly = cleanText
        .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
        .replace(/\n{3,}/g, '\n\n')
        .trim();

    // Check if we have inline images (DOM elements or tracked URLs)
    const existingImages = stream.element.querySelectorAll('.inline-image');
    const trackedImages = stream.inlineImages || [];
    const hasImages = existingImages.length > 0 || trackedImages.length > 0;

    if (!textOnly && !hasImages) {
        stream.element.remove();
        delete streamingMessages[modelId];
        return;
    }
    const contentEl = stream.element.querySelector('.content');
    let html = textOnly ? renderMarkdown(textOnly) : '';
    contentEl.innerHTML = html;
    // Re-append any inline images that were added during streaming
    for (const img of existingImages) {
        contentEl.appendChild(img);
    }
    // Also create img elements from tracked URLs (fallback if DOM elements were lost)
    if (existingImages.length === 0 && trackedImages.length > 0) {
        for (const url of trackedImages) {
            const img = document.createElement('img');
            img.src = url;
            img.alt = 'Generated image';
            img.className = 'inline-image';
            img.onload = () => forceScrollToBottom();
            img.onerror = function() {
                this.dataset.retries = (this.dataset.retries || 0);
                if (this.dataset.retries < 3) {
                    this.dataset.retries++;
                    setTimeout(() => this.src = this.src, 1000);
                } else {
                    this.style.opacity = '0.3';
                }
            };
            contentEl.appendChild(img);
        }
    }

    stream.element.querySelectorAll('pre code').forEach((block) => {
        hljs.highlightElement(block);
    });

    delete streamingMessages[modelId];
}

// ═══ Rendering ═══

function renderMarkdown(text) {
    if (!text) return '';
    try {
        let html = marked.parse(text);
        html = html.replace(/<img /g, '<img loading="lazy" onload="forceScrollToBottom()" onerror="this.dataset.retries=(this.dataset.retries||0);if(this.dataset.retries<3){this.dataset.retries++;setTimeout(()=>this.src=this.src,1000)}else{this.style.opacity=\'0.3\'}" ');
        return html;
    } catch (e) {
        return escapeHtml(text);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ═══ History ═══

function cleanHistoryContent(text) {
    return text
        .replace(/\[\s*\{\s*"name"\s*:\s*"[^"]*image[^"]*"[\s\S]*?\}\s*\]/gi, '')
        .replace(/\{\s*"name"\s*:\s*"[^"]*image[^"]*"\s*,\s*"arguments"\s*:\s*\{[\s\S]*?\}\s*\}/gi, '')
        // Strip {"generate_image": "..."} and any JSON with "prompt" or "generate" key
        .replace(/\{\s*"generate_image"\s*:\s*"[^"]*"\s*\}/gi, '')
        .replace(/\{[^{}]*"[^"]*(?:prompt|generate)[^"]*"\s*:[^{}]*(?:\{[^{}]*\}[^{}]*)*\}/gi, '')
        .replace(/```[^`]*"[^"]*(?:prompt|generate_image)[^"]*"\s*:[^`]*```/gi, '')
        .replace(/^\s*Generate\s+image\.?\s*$/gmi, '')
        .replace(/\[minstrel\]\s*/gi, '')
        .replace(/^\s*\[\w+\]\s*\n?/gm, '')
        // Strip empty code blocks and horizontal rules left behind
        .replace(/```\s*```/g, '')
        .replace(/^\s*```\s*$/gm, '')
        .replace(/^\s*---+\s*$/gm, '')
        .replace(/^\s*\*\*\*+\s*$/gm, '')
        .replace(/^\s*___+\s*$/gm, '')
        .replace(/\n{3,}/g, '\n\n')
        .trim();
}

function loadHistory(history) {
    if (!history || !history.length) return;
    const container = document.getElementById('messages');
    container.innerHTML = '';

    let i = 0;
    const batchSize = 3;
    function renderBatch() {
        const end = Math.min(i + batchSize, history.length);
        const fragment = document.createDocumentFragment();
        for (; i < end; i++) {
            const msg = history[i];
            const color = msg.role === 'user' ? '#00b4d8' : (activeModels[msg.role]?.color || '#888');
            let cleaned = msg.role === 'user' ? msg.content : cleanHistoryContent(msg.content);
            // Strip ALL image markdown from text — we render real images separately
            const textOnly = cleaned.replace(/!\[[^\]]*\]\([^)]*\)/g, '').replace(/\n{3,}/g, '\n\n').trim();
            // Get real image URLs from the images array (server-verified)
            const realImages = msg.images || [];
            if (!textOnly && realImages.length === 0) continue;
            const el = createMessageElement(msg.name, textOnly, color);
            // Append only real server-generated images
            if (realImages.length > 0) {
                const contentEl = el.querySelector('.content');
                for (const url of realImages) {
                    const img = document.createElement('img');
                    img.src = url;
                    img.alt = 'Generated image';
                    img.className = 'inline-image';
                    img.onload = () => forceScrollToBottom();
                    img.onerror = function() {
                        this.dataset.retries = (this.dataset.retries || 0);
                        if (this.dataset.retries < 3) {
                            this.dataset.retries++;
                            setTimeout(() => this.src = this.src, 1000);
                        } else {
                            this.style.opacity = '0.3';
                        }
                    };
                    contentEl.appendChild(img);
                }
            }
            fragment.appendChild(el);
        }
        container.appendChild(fragment);
        if (i < history.length) {
            requestAnimationFrame(renderBatch);
        } else {
            scrollToBottom();
        }
    }
    renderBatch();
}

// ═══ Input ═══

function sendMessage() {
    const input = document.getElementById('user-input');
    const content = input.value.trim();
    if (!content) return;

    if (safeSend({ action: 'send_message', content: content })) {
        input.value = '';
        input.style.height = 'auto';
        // Flash the send button
        const btn = document.querySelector('.btn-send');
        if (btn) {
            btn.classList.add('sent-flash');
            setTimeout(() => btn.classList.remove('sent-flash'), 400);
        }
        // Brief input bar glow
        const bar = document.getElementById('input-bar');
        if (bar) {
            bar.classList.add('sent-glow');
            setTimeout(() => bar.classList.remove('sent-glow'), 600);
        }
        // Force scroll so thinking indicators are visible below user message
        userScrolled = false;
        forceScrollToBottom();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('user-input');
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // Auto-resize textarea
    input.addEventListener('input', () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 200) + 'px';
    });

    // Escape to kill (or close lightbox/settings)
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            // Close settings modal first
            const settings = document.getElementById('settings-overlay-modal');
            if (settings && settings.classList.contains('open')) {
                closeSettings();
                return;
            }
            const lb = document.querySelector('.lightbox-overlay');
            if (lb) { lb.remove(); return; }
            sendKill();
        }
    });

    // Click images to open lightbox
    document.getElementById('messages').addEventListener('click', (e) => {
        if (e.target.tagName === 'IMG' && e.target.closest('.message .content')) {
            const overlay = document.createElement('div');
            overlay.className = 'lightbox-overlay';
            overlay.innerHTML = `<img src="${e.target.src}" alt="Full size">`;
            overlay.addEventListener('click', () => overlay.remove());
            document.body.appendChild(overlay);
        }
    });

    // Auto-refresh models when host input changes (debounced)
    const hostInput = document.getElementById('ollama-host');
    let hostTimer = null;
    hostInput.addEventListener('input', () => {
        clearTimeout(hostTimer);
        hostTimer = setTimeout(() => refreshModels(), 800);
    });

    // Init slider fills for the Add Model form
    initAllSliderFills(document.getElementById('add-model-section'));

    connect();
});

// ═══ Controls ═══

function saveProfile() {
    const profile = document.getElementById('user-profile').value;
    safeSend({ action: 'update_profile', profile: profile });
    // Visual feedback
    const btn = event.target;
    const orig = btn.textContent;
    btn.textContent = 'Saved!';
    btn.style.color = 'var(--success)';
    setTimeout(() => { btn.textContent = orig; btn.style.color = ''; }, 1500);
}

function toggleAutoChat() {
    const toggle = document.getElementById('auto-chat-toggle');
    const promptEl = document.getElementById('auto-chat-prompt');
    if (toggle.checked) {
        const prompt = promptEl ? promptEl.value.trim() : '';
        safeSend({ action: 'auto_chat_start', prompt: prompt });
        if (prompt && promptEl) promptEl.value = '';
        if (promptEl) promptEl.classList.remove('visible');
    } else {
        safeSend({ action: 'auto_chat_stop' });
        if (promptEl) promptEl.classList.remove('visible');
    }
}

function toggleSection(header) {
    const content = header.nextElementSibling;
    const icon = header.querySelector('.collapse-icon');
    content.classList.toggle('collapsed');
    icon.textContent = content.classList.contains('collapsed') ? '+' : '\u2212';
    // Init slider fills when expanding
    if (!content.classList.contains('collapsed')) {
        initAllSliderFills(content);
    }
}

let autoScrollEnabled = true;

function toggleAutoScroll() {
    const toggle = document.getElementById('auto-scroll-toggle');
    autoScrollEnabled = toggle.checked;
    if (autoScrollEnabled) {
        userScrolled = false;
        scrollToBottom();
    }
}

function updateStageInterval(input) {
    const val = parseFloat(input.value);
    const numInput = document.getElementById('stage-interval-num');
    if (numInput) numInput.value = val;
    const label = document.getElementById('stage-interval-val');
    if (label) label.textContent = 'min';
    updateSliderFill(input);
    safeSend({ action: 'update_stage_interval', minutes: val });
}

function setStageIntervalNum(input) {
    let val = parseFloat(input.value) || 2;
    val = Math.max(0.5, Math.min(720, val));
    input.value = val;
    const slider = document.getElementById('stage-interval');
    if (slider) {
        slider.value = Math.min(val, 60); // slider maxes at 60
        updateSliderFill(slider);
    }
    safeSend({ action: 'update_stage_interval', minutes: val });
}

function updateAutoChatButton() {
    const toggle = document.getElementById('auto-chat-toggle');
    if (toggle) {
        toggle.checked = autoChatActive;
    }
    const promptEl = document.getElementById('auto-chat-prompt');
    if (promptEl) {
        if (autoChatActive) {
            promptEl.classList.remove('visible');
        }
    }
}

function showStageIndicator(current, total, text) {
    let indicator = document.getElementById('stage-indicator');
    if (!indicator) {
        indicator = document.createElement('div');
        indicator.id = 'stage-indicator';
        indicator.className = 'stage-indicator';
        const chatArea = document.querySelector('.chat-area');
        if (chatArea) chatArea.appendChild(indicator);
    }
    const pct = (current / total) * 100;
    indicator.innerHTML = `
        <div class="stage-progress-bar"><div class="stage-progress-fill" style="width:${pct}%"></div></div>
        <div class="stage-label">
            <span class="stage-badge">STAGE ${current}/${total}</span>
            <span class="stage-text">${text}</span>
        </div>
    `;
    indicator.style.display = 'flex';
}

function hideStageIndicator() {
    const indicator = document.getElementById('stage-indicator');
    if (indicator) indicator.style.display = 'none';
}

// ═══ Stages Schedule Display ═══

let _stagesTimerInterval = null;

function showStagesSchedule(stages, intervalMinutes, currentStage) {
    // Clear any existing timer
    if (_stagesTimerInterval) clearInterval(_stagesTimerInterval);

    // Use the pinned container at the bottom of the chat area
    const scheduleEl = document.getElementById('stages-schedule-pinned');
    if (!scheduleEl) return;
    scheduleEl.style.display = '';

    const intervalSec = intervalMinutes * 60;
    const startTime = Date.now();

    let collapsed = false;

    function render() {
        const elapsed = (Date.now() - startTime) / 1000;
        // Auto-hide when all stages are done
        if (currentStage > stages.length) {
            scheduleEl.style.display = 'none';
            scheduleEl.innerHTML = '';
            if (_stagesTimerInterval) { clearInterval(_stagesTimerInterval); _stagesTimerInterval = null; }
            return;
        }

        // Active stage timer
        const secsUntilNext = Math.max(0, intervalSec - elapsed);
        const am = Math.floor(secsUntilNext / 60);
        const as = Math.floor(secsUntilNext % 60);
        const activeTimer = am > 0 ? `${am}m ${as}s` : `${as}s`;

        // Collapsed: compact one-liner
        const toggleIcon = collapsed ? '&#9662;' : '&#9652;';
        let html = `<div class="schedule-header" onclick="_toggleScheduleCollapse()" style="cursor:pointer;margin-bottom:${collapsed ? '0' : '10px'}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            Stage ${currentStage}/${stages.length}
            ${_autoKillEnabled && _autoKillTimer ? '<span style="margin-left:4px;color:rgba(255,45,85,0.5);font-size:8px;">auto-stop on</span>' : ''}
            <span style="margin-left:auto;color:var(--accent);font-size:10px;">${activeTimer}</span>
            <span style="margin-left:8px;font-size:12px;-webkit-text-fill-color:var(--text-muted);color:var(--text-muted)">${toggleIcon}</span>
        </div>`;

        if (!collapsed) {
            html += '<div class="schedule-stages">';
            stages.forEach((text, i) => {
                const stageNum = i + 1;
                const isDone = stageNum < currentStage;
                const isActive = stageNum === currentStage;
                let status = '';
                let timerText = '';

                if (isDone) {
                    status = 'done';
                    const sentAt = new Date(startTime).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
                    timerText = 'sent ' + sentAt;
                } else if (isActive) {
                    status = 'active';
                    timerText = 'next in ' + activeTimer;
                } else {
                    status = 'pending';
                    const secsUntilThis = Math.max(0, intervalSec * (stageNum - currentStage) - elapsed);
                    const m = Math.floor(secsUntilThis / 60);
                    const s = Math.floor(secsUntilThis % 60);
                    timerText = m > 0 ? `in ~${m}m ${s}s` : `in ~${s}s`;
                }

                html += `<div class="schedule-stage ${status}">
                    <span class="schedule-num">${stageNum}</span>
                    <span class="schedule-text">${escapeHtml(text)}</span>
                    <span class="schedule-timer">${timerText}</span>
                </div>`;
            });
            html += '</div>';
        }

        scheduleEl.innerHTML = html;
    }

    // Expose toggle for onclick
    window._toggleScheduleCollapse = () => { collapsed = !collapsed; render(); };

    render();
    _stagesTimerInterval = setInterval(render, 1000);
    scrollToBottom();
}

// ═══ Auto-Kill Timer ═══

let _autoKillEnabled = false;
let _autoKillMinutes = 30;
let _autoKillTimer = null;

function toggleAutoKill() {
    const toggle = document.getElementById('auto-kill-toggle');
    _autoKillEnabled = toggle.checked;
    const settingsDiv = document.getElementById('auto-kill-settings');
    if (settingsDiv) settingsDiv.style.display = _autoKillEnabled ? '' : 'none';
    if (_autoKillEnabled) {
        initAllSliderFills(settingsDiv);
    }
    // Clear any existing timer
    if (_autoKillTimer) { clearTimeout(_autoKillTimer); _autoKillTimer = null; }
}

function updateAutoKillDuration(input) {
    _autoKillMinutes = parseInt(input.value);
    const numInput = document.getElementById('auto-kill-num');
    if (numInput) numInput.value = _autoKillMinutes;
    updateSliderFill(input);
}

function setAutoKillNum(input) {
    let val = parseInt(input.value) || 30;
    val = Math.max(1, Math.min(720, val));
    input.value = val;
    _autoKillMinutes = val;
    const slider = document.getElementById('auto-kill-duration');
    if (slider) {
        slider.value = Math.min(val, 120);
        updateSliderFill(slider);
    }
}

function startAutoKillTimer() {
    if (!_autoKillEnabled) return;
    if (_autoKillTimer) clearTimeout(_autoKillTimer);
    _autoKillTimer = setTimeout(() => {
        addSystemMessage(`Auto-stop: ${_autoKillMinutes}min timer reached. Killing all.`);
        sendKill();
        _autoKillTimer = null;
    }, _autoKillMinutes * 60 * 1000);
}

function sendKill() {
    safeSend({ action: 'kill' });
    if (_autoKillTimer) { clearTimeout(_autoKillTimer); _autoKillTimer = null; }
}

function clearHistory() {
    safeSend({ action: 'clear_history' });
}

// ═══ Stage Builder ═══

let _sbStages = [{ text: '', subs: [] }, { text: '', subs: [] }];

function spinTime(inputId, delta, min, max) {
    const el = document.getElementById(inputId);
    if (!el) return;
    let val = parseInt(el.value || '0') + delta;
    if (val < min) val = min;
    if (val > max) val = max;
    el.value = val;
}

function openStageBuilder() {
    const modal = document.getElementById('stage-builder-modal');
    if (!modal) return;
    _sbStages = [{ text: '', subs: [] }, { text: '', subs: [] }];
    renderBuilderStages();
    modal.classList.add('open');
}

function closeStageBuilder() {
    const modal = document.getElementById('stage-builder-modal');
    if (modal) modal.classList.remove('open');
}

function addBuilderStage() {
    _sbStages.push({ text: '', subs: [] });
    renderBuilderStages();
    const list = document.getElementById('stage-builder-list');
    const cards = list.querySelectorAll('.stage-builder-card');
    const lastCard = cards[cards.length - 1];
    if (lastCard) lastCard.querySelector('textarea')?.focus();
}

function removeBuilderStage(idx) {
    if (_sbStages.length <= 2) return;
    _sbStages.splice(idx, 1);
    renderBuilderStages();
}

function moveBuilderStage(idx, dir) {
    const newIdx = idx + dir;
    if (newIdx < 0 || newIdx >= _sbStages.length) return;
    [_sbStages[idx], _sbStages[newIdx]] = [_sbStages[newIdx], _sbStages[idx]];
    renderBuilderStages();
}

function saveBuilderStageText(idx, value) {
    _sbStages[idx].text = value;
}

function addSubEvent(stageIdx) {
    _sbStages[stageIdx].subs.push('');
    renderBuilderStages();
    // Focus new sub textarea
    const card = document.querySelectorAll('.stage-builder-card')[stageIdx];
    if (card) {
        const subs = card.querySelectorAll('.sub-event-input');
        if (subs.length) subs[subs.length - 1].focus();
    }
}

function removeSubEvent(stageIdx, subIdx) {
    _sbStages[stageIdx].subs.splice(subIdx, 1);
    renderBuilderStages();
}

function saveSubEventText(stageIdx, subIdx, value) {
    _sbStages[stageIdx].subs[subIdx] = value;
}

function renderBuilderStages() {
    const list = document.getElementById('stage-builder-list');
    if (!list) return;
    list.innerHTML = '';
    _sbStages.forEach((stage, i) => {
        const card = document.createElement('div');
        card.className = 'stage-builder-card';

        let subsHtml = '';
        if (stage.subs.length > 0) {
            subsHtml = '<div class="sub-events-list">';
            stage.subs.forEach((sub, si) => {
                const letter = String.fromCharCode(97 + si); // a, b, c...
                subsHtml += `
                    <div class="sub-event-row">
                        <span class="sub-event-label">${i + 1}${letter}</span>
                        <input type="text" class="sub-event-input" placeholder="Sub-event ${letter}..." value="${escapeHtml(sub)}" oninput="saveSubEventText(${i},${si},this.value)">
                        <button class="sub-event-remove" onclick="removeSubEvent(${i},${si})" title="Remove">&times;</button>
                    </div>`;
            });
            subsHtml += '</div>';
        }

        card.innerHTML = `
            <div class="stage-builder-main">
                <span class="stage-builder-num">${i + 1}</span>
                <textarea rows="2" placeholder="Stage ${i + 1} prompt..." oninput="saveBuilderStageText(${i}, this.value)">${escapeHtml(stage.text)}</textarea>
                <div class="stage-builder-actions">
                    <button onclick="moveBuilderStage(${i},-1)" title="Move up">${i > 0 ? '&#9650;' : ''}</button>
                    <button onclick="moveBuilderStage(${i},1)" title="Move down">${i < _sbStages.length - 1 ? '&#9660;' : ''}</button>
                    <button class="sb-remove" onclick="removeBuilderStage(${i})" title="Remove">${_sbStages.length > 2 ? '&times;' : ''}</button>
                </div>
            </div>
            ${subsHtml}
            <button class="sub-event-add" onclick="addSubEvent(${i})">+ sub</button>
        `;
        list.appendChild(card);
    });
}

function _flattenStages() {
    // Flatten stages with sub-events into a linear list for the server
    // Each stage becomes: [main, sub_a, sub_b, ...] with fractional intervals
    const result = [];
    for (const stage of _sbStages) {
        const mainText = stage.text.trim();
        if (!mainText) continue;
        const validSubs = stage.subs.map(s => s.trim()).filter(s => s.length > 0);
        if (validSubs.length === 0) {
            result.push({ prompts: [mainText], count: 1 });
        } else {
            result.push({ prompts: [mainText, ...validSubs], count: 1 + validSubs.length });
        }
    }
    return result;
}

function startStageBuilder() {
    const stageGroups = _flattenStages();
    if (stageGroups.length < 2) {
        addSystemMessage('Need at least 2 stages to start.');
        return;
    }

    const intHr = parseInt(document.getElementById('sb-int-hr')?.value || '0');
    const intMin = parseInt(document.getElementById('sb-int-min')?.value || '2');
    const interval = intHr * 60 + intMin;
    if (interval < 1) {
        addSystemMessage('Stage interval must be at least 1 minute.');
        return;
    }
    const autoKillEnabled = document.getElementById('sb-auto-kill')?.checked || false;
    const killHr = parseInt(document.getElementById('sb-kill-hr')?.value || '0');
    const killMin = parseInt(document.getElementById('sb-kill-min')?.value || '30');
    const autoKillMinutes = autoKillEnabled ? (killHr * 60 + killMin) : 0;

    // Flatten into linear prompts with per-prompt intervals
    // Each stage group divides the interval by its prompt count
    const flatStages = [];
    const flatIntervals = [];
    for (const group of stageGroups) {
        const subInterval = interval / group.count;
        for (const prompt of group.prompts) {
            flatStages.push(prompt);
            flatIntervals.push(subInterval);
        }
    }

    safeSend({
        action: 'send_stages',
        stages: flatStages,
        intervals: flatIntervals,
        interval_minutes: Math.max(0.5, interval),
        auto_kill_after_last: autoKillMinutes > 0 ? autoKillMinutes : null,
    });

    closeStageBuilder();
}

// ═══ Export Options Modal ═══

const _ico = {
    cyber: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="3" x2="9" y2="21"/><circle cx="16" cy="16" r="2"/></svg>',
    bbs: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/><line x1="6" y1="7" x2="10" y2="7"/><line x1="6" y1="10" x2="14" y2="10"/><line x1="6" y1="13" x2="11" y2="13"/></svg>',
    clean: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
    neon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>',
    comic: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 2L14.5 9H22L16 13.5L18 21L12 17L6 21L8 13.5L2 9H9.5L12 2Z"/></svg>',
    terminal: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
};

const STORYBOOK_TEMPLATES = [
    { id: 'cyberpunk', name: 'Cyberpunk Noir', icon: _ico.cyber, desc: 'Dark neon gradients', colors: { accent: '#00ffc8', highlight: '#bd00ff', bg: '#0a0a0f', text: '#d4d4d8' } },
    { id: 'bbs', name: 'BBS Terminal', icon: _ico.bbs, desc: 'Green phosphor retro', colors: { accent: '#33ff33', highlight: '#1a5a1a', bg: '#0a0a00', text: '#33ff33' } },
    { id: 'clean', name: 'Clean Light', icon: _ico.clean, desc: 'Minimal white serif', colors: { accent: '#1a1a1a', highlight: '#555555', bg: '#fafafa', text: '#2a2a2a' } },
    { id: 'neon', name: 'Neon Dreams', icon: _ico.neon, desc: 'Heavy glow effects', colors: { accent: '#00ffff', highlight: '#ff0080', bg: '#05050a', text: '#e0d0ff' } },
];

const FLIPBOOK_TEMPLATES = [
    { id: 'cyber', name: 'Cyber', icon: _ico.cyber, desc: 'Neon Ken Burns', colors: { accent: '#00ffc8', highlight: '#bd00ff', bg: '#000000', text: '#e8e8ea' } },
    { id: 'comic', name: 'Comic', icon: _ico.comic, desc: 'Bold borders, halftone', colors: { accent: '#ffee33', highlight: '#ff3366', bg: '#1a1a2e', text: '#1a1a2e' } },
    { id: 'terminal', name: 'Terminal', icon: _ico.terminal, desc: 'Scanlines, green CRT', colors: { accent: '#33ff33', highlight: '#1a5a1a', bg: '#0a0a00', text: '#33ff33' } },
    { id: 'clean', name: 'Clean', icon: _ico.clean, desc: 'Minimal, no effects', colors: { accent: '#ffffff', highlight: '#cccccc', bg: '#111111', text: '#ffffff' } },
];

let _exportState = { type: null, sessionId: null, template: null };

function openExportModal(type, sessionId) {
    _exportState = { type, sessionId, template: null };
    const modal = document.getElementById('export-modal');
    const title = document.getElementById('export-modal-title');
    const templatesEl = document.getElementById('export-templates');
    const sbOpts = document.getElementById('export-storybook-options');
    const fbOpts = document.getElementById('export-flipbook-options');

    title.textContent = type === 'storybook' ? 'Export Storybook' : 'Export Flip Book';
    sbOpts.style.display = type === 'storybook' ? '' : 'none';
    fbOpts.style.display = type === 'flipbook' ? '' : 'none';

    // Reset shared fields
    document.getElementById('export-custom-title').value = '';
    document.getElementById('export-custom-subtitle').value = '';
    document.getElementById('export-prompt-title').checked = false;

    const templates = type === 'storybook' ? STORYBOOK_TEMPLATES : FLIPBOOK_TEMPLATES;
    templatesEl.innerHTML = '';
    function _setExportColors(colors) {
        document.getElementById('export-color-accent').value = colors.accent;
        document.getElementById('export-color-highlight').value = colors.highlight;
        document.getElementById('export-color-bg').value = colors.bg;
        document.getElementById('export-color-text').value = colors.text;
    }
    templates.forEach((t, i) => {
        const card = document.createElement('div');
        card.className = 'export-template-card' + (i === 0 ? ' selected' : '');
        card.innerHTML = `<span class="template-icon">${t.icon}</span><div class="template-name">${t.name}</div><div class="template-desc">${t.desc}</div>`;
        card.onclick = () => {
            templatesEl.querySelectorAll('.export-template-card').forEach(c => c.classList.remove('selected'));
            card.classList.add('selected');
            _exportState.template = t.id;
            _setExportColors(t.colors);
        };
        templatesEl.appendChild(card);
    });
    _exportState.template = templates[0].id;
    _setExportColors(templates[0].colors);

    // Init slider fills and speed display
    const speedSlider = document.getElementById('export-speed');
    if (speedSlider) {
        updateFlipbookSpeed(speedSlider);
    }

    modal.classList.add('open');
}

function closeExportModal() {
    document.getElementById('export-modal').classList.remove('open');
}

// Convert 0-100 slider to seconds: 0=20s (slow), 100=0.2s (movie speed)
function _sliderToSeconds(val) {
    // Exponential curve: 20 * (0.01 ^ (val/100)) = 20 at 0, 0.2 at 100
    return Math.max(0.2, 20 * Math.pow(0.01, val / 100));
}

function updateFlipbookSpeed(input) {
    const secs = _sliderToSeconds(parseInt(input.value));
    document.getElementById('export-speed-val').textContent = secs.toFixed(1);
    updateSliderFill(input);
}

function confirmExport() {
    const { type, sessionId, template } = _exportState;
    if (!sessionId) return;
    const params = new URLSearchParams();
    if (template) params.set('template', template);

    // Shared title/subtitle options
    const customTitle = document.getElementById('export-custom-title').value.trim();
    const customSubtitle = document.getElementById('export-custom-subtitle').value.trim();
    if (customTitle) params.set('title', customTitle);
    if (customSubtitle) params.set('subtitle', customSubtitle);
    if (document.getElementById('export-prompt-title').checked) params.set('prompt_title', '1');

    // Colors
    params.set('color_accent', document.getElementById('export-color-accent').value);
    params.set('color_highlight', document.getElementById('export-color-highlight').value);
    params.set('color_bg', document.getElementById('export-color-bg').value);
    params.set('color_text', document.getElementById('export-color-text').value);

    if (type === 'storybook') {
        if (document.getElementById('export-image-captions').checked) params.set('captions', '1');
    } else if (type === 'flipbook') {
        const sliderVal = parseInt(document.getElementById('export-speed').value);
        const seconds = _sliderToSeconds(sliderVal);
        params.set('speed', seconds.toFixed(1));
        if (!document.getElementById('export-show-text').checked) params.set('hide_text', '1');
    }

    const qs = params.toString();
    const url = `/${type}/${sessionId}${qs ? '?' + qs : ''}`;
    window.open(url, '_blank');
    closeExportModal();
}

function exportStorybook() {
    if (!currentSessionId) return;
    openExportModal('storybook', currentSessionId);
}

function exportSessionStorybook(sessionId) {
    closeAllSessionMenus();
    openExportModal('storybook', sessionId);
}

function exportSessionFlipbook(sessionId) {
    closeAllSessionMenus();
    openExportModal('flipbook', sessionId);
}

function exportSessionPrompts(sessionId) {
    closeAllSessionMenus();
    window.open(`/prompts/${sessionId}`, '_blank');
}

function toggleSessionMenu(sessionId, btn) {
    const menu = document.getElementById(`session-menu-${sessionId}`);
    const wasOpen = menu.classList.contains('open');
    closeAllSessionMenus();
    if (!wasOpen) {
        // Position menu to the right of the gear button so it doesn't overlap session text
        const rect = btn.getBoundingClientRect();
        const menuWidth = 180;
        let left = rect.right + 6;
        // If it would go off-screen, flip to left side
        if (left + menuWidth > window.innerWidth - 8) {
            left = rect.left - menuWidth - 6;
        }
        menu.style.top = rect.top + 'px';
        menu.style.left = Math.max(8, left) + 'px';
        menu.classList.add('open');
        // Close on outside click
        setTimeout(() => {
            document.addEventListener('click', closeAllSessionMenus, { once: true });
        }, 0);
    }
}

function closeAllSessionMenus() {
    document.querySelectorAll('.session-menu.open').forEach(m => m.classList.remove('open'));
}

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    sidebar.classList.toggle('open');
    overlay.classList.toggle('open');
}

// ═══ SD Settings ═══

function loadSdSettings(settings) {
    if (!settings) return;
    const section = document.getElementById('sd-settings-section');
    if (section) section.style.display = '';

    const setSlider = (id, valId, val) => {
        const el = document.getElementById(id);
        const valEl = document.getElementById(valId);
        if (el) { el.value = val; updateSliderFill(el); }
        if (valEl) valEl.textContent = val;
    };

    setSlider('sd-steps', 'sd-steps-val', settings.steps);
    setSlider('sd-guidance', 'sd-cfg-val', settings.guidance_scale);
    setSlider('sd-width', 'sd-width-val', settings.width);
    setSlider('sd-height', 'sd-height-val', settings.height);

    const negEl = document.getElementById('sd-negative-prompt');
    if (negEl) negEl.value = settings.negative_prompt || '';

    // Update preset buttons
    updateSdPresetButtons(settings.width, settings.height);
}

let _sdUpdateTimers = {};
function updateSdSetting(key, value) {
    clearTimeout(_sdUpdateTimers[key]);
    _sdUpdateTimers[key] = setTimeout(() => {
        const msg = { action: 'update_sd_settings' };
        msg[key] = value;
        safeSend(msg);
    }, 300);
    // Update preset highlights for dimension changes
    if (key === 'width' || key === 'height') {
        const w = parseInt(document.getElementById('sd-width').value);
        const h = parseInt(document.getElementById('sd-height').value);
        updateSdPresetButtons(w, h);
    }
}

function setSdPreset(w, h) {
    const wEl = document.getElementById('sd-width');
    const hEl = document.getElementById('sd-height');
    if (wEl) { wEl.value = w; updateSliderFill(wEl); }
    if (hEl) { hEl.value = h; updateSliderFill(hEl); }
    document.getElementById('sd-width-val').textContent = w;
    document.getElementById('sd-height-val').textContent = h;
    updateSdPresetButtons(w, h);
    safeSend({ action: 'update_sd_settings', width: w, height: h });
}

function updateSdPresetButtons(w, h) {
    document.querySelectorAll('.sd-preset').forEach(btn => {
        btn.classList.remove('active');
    });
    const presets = [[512,512],[768,768],[1024,1024],[1024,768],[768,1024]];
    const labels = ['512²','768²','1024²','1024×768','768×1024'];
    const idx = presets.findIndex(p => p[0] === w && p[1] === h);
    if (idx >= 0) {
        const btns = document.querySelectorAll('.sd-preset');
        if (btns[idx]) btns[idx].classList.add('active');
    }
}

// ═══ Scroll ═══

let userScrolled = false;

document.addEventListener('DOMContentLoaded', () => {
    const messages = document.getElementById('messages');
    messages.addEventListener('scroll', () => {
        const atBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 50;
        userScrolled = !atBottom;
    });
});

function scrollToBottom() {
    if (!autoScrollEnabled || userScrolled) return;
    const messages = document.getElementById('messages');
    messages.scrollTop = messages.scrollHeight;
}

function forceScrollToBottom() {
    if (!autoScrollEnabled) return;
    userScrolled = false;
    const messages = document.getElementById('messages');
    messages.scrollTop = messages.scrollHeight;
}
