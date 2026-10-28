/**
 * NextGenUp — AI Models controller: registry manager (download / update /
 * remove), GPU engine install, and a shared capabilities helper exposed as
 * window.NGUModels for the other controllers.
 */
(function () {
    'use strict';

    const $ = (sel) => document.querySelector(sel);
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

    // Legacy builtin models used by the restore/audio tools — managed by
    // those tools themselves, so Remove is not offered here.
    const LEGACY_KEYS = new Set([
        'bg', 'denoise', 'deblur', 'colorize', 'erase', 'separate', 'denoise_speech',
    ]);

    const GROUPS = [
        { kind: 'image-sr', title: 'Image Models', el: () => $('#models-image-list') },
        { kind: 'video-sr', title: 'Video Models', el: () => $('#models-video-list') },
        { kind: 'other', title: 'Audio & Utility Models', el: () => $('#models-audio-list') },
    ];

    const state = {
        models: [],
        capabilities: null,
        refreshing: false,
        engineInstalling: false,
    };

    // --- Shared helpers (used by image-app.js / app.js too) ---

    let capsPromise = null;
    function getCapabilities(force) {
        if (force || !capsPromise) {
            capsPromise = fetch('/api/capabilities')
                .then((r) => r.json())
                .catch(() => null);
        }
        return capsPromise;
    }

    async function getModels() {
        const res = await fetch('/api/models');
        const data = await res.json();
        state.models = data.models || [];
        return state.models;
    }

    window.NGUModels = { getCapabilities, getModels };

    // --- UI helpers ---

    function fmtSize(bytes) {
        if (!bytes && bytes !== 0) return '—';
        if (bytes > 1024 * 1024 * 1024) return (bytes / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
        if (bytes > 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        return Math.max(1, Math.round(bytes / 1024)) + ' KB';
    }

    function isGpuModel(m) {
        return m.engine && m.engine !== 'onnx';
    }

    function engineLabel(engine) {
        return { 'onnx-gpu': 'onnx-gpu', 'torch-swin2sr': 'torch',
                 'seedvr2': 'seedvr2', 'ltx-upscaler': 'ltx' }[engine] || engine;
    }

    // --- Rendering ---

    function modelRow(m) {
        const row = document.createElement('div');
        row.className = 'model-row';

        // Name + label
        const name = document.createElement('div');
        name.className = 'model-name';
        const label = document.createElement('span');
        label.className = 'model-label';
        label.textContent = m.label || m.key;
        const key = document.createElement('span');
        key.className = 'model-key';
        key.textContent = m.key;
        name.append(label, key);
        row.appendChild(name);

        // Engine badge
        const engine = document.createElement('span');
        engine.className = 'engine-badge' + (isGpuModel(m) ? ' engine-gpu' : '');
        engine.textContent = engineLabel(m.engine);
        row.appendChild(engine);

        // Size
        const size = document.createElement('span');
        size.className = 'model-size';
        size.textContent = fmtSize(m.size_bytes || m.size);
        row.appendChild(size);

        // State
        row.appendChild(stateCell(m));

        // Actions
        const actions = document.createElement('div');
        actions.className = 'model-actions';
        if (m.status === 'downloading') {
            // nothing clickable while downloading
        } else if (m.status === 'ready') {
            if (m.source && m.source.revision === 'main') {
                actions.appendChild(actionBtn('Update', 'model-btn', () => doPost('/api/models', { action: 'update', key: m.key })));
            }
            if (!LEGACY_KEYS.has(m.key)) {
                actions.appendChild(actionBtn('Remove', 'model-btn model-btn-danger', () => doPost('/api/models', { action: 'remove', key: m.key })));
            }
        } else if (m.status === 'error') {
            actions.appendChild(actionBtn('Retry', 'model-btn', () => doPost('/api/models', { action: 'download', key: m.key })));
        } else if (!m.available) {
            actions.appendChild(actionBtn('Download', 'model-btn', () => doPost('/api/models', { action: 'download', key: m.key })));
        }
        row.appendChild(actions);
        return row;
    }

    function stateCell(m) {
        const cell = document.createElement('div');
        cell.className = 'model-state';
        if (m.status === 'downloading') {
            cell.innerHTML = '<span class="model-state-text state-downloading">Downloading ' +
                (m.progress || 0) + '%</span>' +
                '<div class="model-progress-track"><div class="model-progress-fill" style="width:' +
                (m.progress || 0) + '%"></div></div>';
        } else if (m.status === 'error') {
            const t = document.createElement('span');
            t.className = 'model-state-text state-error';
            t.textContent = 'Error';
            t.title = m.error || 'Download failed';
            cell.appendChild(t);
        } else if (m.available) {
            let text = 'Ready';
            if (isGpuModel(m)) {
                if (m.gpu && !m.gpu.available && m.gpu.reason) {
                    text = 'Ready · ' + m.gpu.reason;
                    cell.classList.add('state-warn-wrap');
                } else if (m.gpu && m.gpu.available) {
                    text = 'Ready · GPU';
                }
            }
            const t = document.createElement('span');
            t.className = 'model-state-text ' + (text === 'Ready · GPU' ? 'state-ready' : 'state-idle');
            t.textContent = text;
            t.title = (m.gpu && m.gpu.reason) || '';
            cell.appendChild(t);
        } else {
            const t = document.createElement('span');
            t.className = 'model-state-text state-idle';
            t.textContent = m.status === 'idle' ? 'Not downloaded' : m.status;
            cell.appendChild(t);
        }
        return cell;
    }

    function actionBtn(text, cls, onClick) {
        const b = document.createElement('button');
        b.className = cls;
        b.textContent = text;
        b.addEventListener('click', onClick);
        return b;
    }

    async function doPost(url, body) {
        try {
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.error || 'Request failed');
            if (body.action === 'update' && data.updated === false) {
                showToast('Already up to date' + (data.reason ? ' — ' + data.reason : ''));
            }
        } catch (e) {
            showToast('Failed: ' + e.message);
        }
        await refresh(false);
    }

    function render() {
        GROUPS.forEach((g) => {
            const list = g.el();
            if (!list) return;
            list.innerHTML = '';
            const items = state.models.filter((m) =>
                g.kind === 'other' ? (m.kind !== 'image-sr' && m.kind !== 'video-sr')
                                   : m.kind === g.kind);
            items.forEach((m) => list.appendChild(modelRow(m)));
            const groupEl = list.closest('.model-group');
            if (groupEl) groupEl.classList.toggle('hidden', items.length === 0);
        });
        renderEnginePanel();
    }

    function renderEnginePanel() {
        const panel = $('#gpu-engine-panel');
        if (!panel) return;
        const caps = state.capabilities;
        const gpu = (caps && caps.gpu) || {};
        const anyGpuModels = state.models.some(isGpuModel);
        panel.classList.toggle('hidden', !anyGpuModels);

        const info = $('#gpu-engine-info');
        const btn = $('#gpu-engine-install-btn');
        const prog = $('#gpu-engine-progress');
        prog.classList.add('hidden');

        let html = '';
        if (gpu.available) {
            html = '<span class="state-ready">GPU: ' + escapeHtml(gpu.name || 'CUDA GPU') +
                (gpu.vram_gb ? ' · ' + gpu.vram_gb + ' GB VRAM' : '') + '</span>';
        } else if (gpu.reason) {
            html = '<span class="state-error" title="' + escapeAttr(gpu.reason) + '">GPU unavailable — ' +
                escapeHtml(gpu.reason) + '</span>';
        } else {
            html = '<span class="state-idle">No CUDA GPU detected</span>';
        }

        const engines = (caps && caps.engines) || {};
        const needed = [...new Set(state.models.filter(isGpuModel).map((m) => m.engine))];
        const missing = needed.filter((e) => engines[e] && !engines[e].installed);
        const allInstalled = needed.length > 0 && needed.every((e) => (engines[e] || {}).installed);

        btn.classList.add('hidden');
        if (missing.length) {
            if (gpu.available) {
                btn.textContent = 'Install GPU engine' + (missing.length > 1 ? 's' : '') +
                    ' (' + missing.map(engineLabel).join(', ') + ')';
                btn.classList.remove('hidden');
                btn.disabled = state.engineInstalling;
            }
        } else if (allInstalled) {
            html += ' <span class="state-ready">· engine ready</span>';
        }
        info.innerHTML = html;
    }

    function escapeHtml(s) {
        return String(s == null ? '' : s).replace(/[&<>"]/g,
            (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    }
    function escapeAttr(s) { return escapeHtml(s).replace(/'/g, '&#39;'); }

    // --- Engine install with live progress ---

    async function installEngine() {
        if (state.engineInstalling) return;
        state.engineInstalling = true;
        const btn = $('#gpu-engine-install-btn');
        const prog = $('#gpu-engine-progress');
        btn.disabled = true;
        try {
            const res = await fetch('/api/engine', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'install' }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.error || 'Install failed');
            while (true) {
                await sleep(1000);
                const st = await (await fetch('/api/engine')).json();
                const task = st.task;
                if (task && task.status === 'processing') {
                    prog.classList.remove('hidden');
                    $('#gpu-engine-progress-fill').style.width = (task.progress || 0) + '%';
                    $('#gpu-engine-progress-text').textContent =
                        (task.message || 'Installing...') + ' — ' + (task.progress || 0) + '%';
                } else if (task && task.status === 'error') {
                    throw new Error(task.error || 'Engine install failed');
                } else if (task && (task.status === 'completed' || task.progress >= 100)) {
                    showToast('GPU engine installed');
                    break;
                }
            }
        } catch (e) {
            showToast('Engine install failed: ' + e.message);
        } finally {
            state.engineInstalling = false;
            prog.classList.add('hidden');
            await refresh(false);
        }
    }

    // --- Refresh / polling ---

    async function refresh(notify) {
        if (state.refreshing) return;
        state.refreshing = true;
        const btn = $('#models-refresh-btn');
        if (btn) { btn.disabled = true; btn.textContent = 'Refreshing…'; }
        try {
            const [, caps] = await Promise.all([getModels(), getCapabilities(true)]);
            state.capabilities = caps;
            render();
            if (notify) showToast('Models refreshed');
        } catch (e) {
            showToast('Refresh failed: ' + e.message);
        } finally {
            state.refreshing = false;
            if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; }
            schedulePoll();
        }
    }

    let pollTimer = null;
    function schedulePoll() {
        clearTimeout(pollTimer);
        const engineBusy = state.engineInstalling ||
            ($('#gpu-engine-progress') && !$('#gpu-engine-progress').classList.contains('hidden'));
        const busy = state.models.some((m) => m.status === 'downloading') || engineBusy;
        if (busy) pollTimer = setTimeout(() => refresh(false), 1000);
    }

    function showToast(msg) {
        const existing = document.querySelector('.toast');
        if (existing) existing.remove();
        const toast = document.createElement('div');
        toast.className = 'toast';
        toast.textContent = msg;
        document.body.appendChild(toast);
        requestAnimationFrame(() => toast.classList.add('show'));
        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => toast.remove(), 300);
        }, 5000);
    }

    function init() {
        $('#models-refresh-btn').addEventListener('click', () => refresh(true));
        $('#gpu-engine-install-btn').addEventListener('click', installEngine);
        refresh(false);
    }

    init();
})();
