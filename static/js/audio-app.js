/**
 * NextGenUp — Audio tools controllers:
 *   Vocal Remover (MDX-Net on the server), Noise Remover (DeepFilterNet3),
 *   Mastering + Tag Editor (ReMaster core, entirely in this window via
 *   the vendored worker — nothing is uploaded).
 */
(function () {
    'use strict';

    const $ = (sel) => document.querySelector(sel);
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const NGU_TOKEN = document.querySelector('meta[name="ngu-token"]')?.content || '';

    // ========================= SHARED HELPERS =========================

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

    function desktopDialog() {
        return (window.__TAURI__ && window.__TAURI__.dialog) ? window.__TAURI__.dialog : null;
    }

    async function browserSaveAs(suggestedName, getBlob) {
        if (!window.showSaveFilePicker) return false;
        let handle;
        try {
            handle = await window.showSaveFilePicker({ suggestedName });
        } catch (e) {
            if (e && e.name === 'AbortError') return true;
            return false;
        }
        try {
            const blob = await getBlob();
            const writable = await handle.createWritable();
            await writable.write(blob);
            await writable.close();
            showToast('Saved ' + suggestedName);
        } catch (e) {
            showToast('Save failed: ' + e.message);
        }
        return true;
    }

    async function saveServerResult(taskId, suggestedName) {
        const dlg = desktopDialog();
        if (dlg) {
            try {
                const path = await dlg.save({ defaultPath: suggestedName });
                if (!path) return;
                const res = await fetch('/api/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-NGU-Token': NGU_TOKEN },
                    body: JSON.stringify({ task_id: taskId, path }),
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || 'save failed');
                showToast('Saved to ' + data.path);
            } catch (e) {
                showToast('Save failed: ' + e.message);
            }
            return;
        }
        const url = `/api/audio/download/${taskId}`;
        if (await browserSaveAs(suggestedName, async () => {
            const r = await fetch(url);
            if (!r.ok) throw new Error('download failed');
            return r.blob();
        })) return;
        const a = document.createElement('a');
        a.href = url;
        a.download = suggestedName;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }

    async function saveBlobResult(blob, suggestedName) {
        const dlg = desktopDialog();
        if (dlg) {
            try {
                const path = await dlg.save({ defaultPath: suggestedName });
                if (!path) return;
                const res = await fetch('/api/save-blob?path=' + encodeURIComponent(path), {
                    method: 'POST',
                    headers: { 'X-NGU-Token': NGU_TOKEN },
                    body: blob,
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || 'save failed');
                showToast('Saved to ' + data.path);
            } catch (e) {
                showToast('Save failed: ' + e.message);
            }
            return;
        }
        if (await browserSaveAs(suggestedName, () => Promise.resolve(blob))) return;
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = suggestedName;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    }

    async function ensureModel(key, onProgress) {
        let s = await (await fetch(`/api/models/${key}`)).json();
        if (s.available) return;
        const mb = Math.round((s.size || 0) / 1e6);
        onProgress(0, `Downloading AI model (${mb} MB) — one-time setup...`);
        await fetch(`/api/models/${key}`, { method: 'POST' });
        while (true) {
            await sleep(1000);
            try {
                s = await (await fetch(`/api/models/${key}`)).json();
            } catch {
                continue;
            }
            if (s.available) return;
            if (s.status === 'error') throw new Error('Model download failed: ' + (s.error || 'network error'));
            onProgress(Math.round((s.progress || 0) * 0.99),
                `Downloading AI model (${mb} MB)... ${s.progress || 0}%`);
        }
    }

    async function uploadAudio(file) {
        const formData = new FormData();
        formData.append('audio', file);
        const res = await fetch('/api/audio/upload', { method: 'POST', body: formData });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Upload failed');
        return data;
    }

    function pollStatus(taskId, onProgress, isCancelled) {
        return new Promise((resolve, reject) => {
            const iv = setInterval(async () => {
                if (isCancelled && isCancelled()) {
                    clearInterval(iv);
                    reject(new Error('cancelled'));
                    return;
                }
                try {
                    const s = await (await fetch(`/api/image/status/${taskId}`)).json();
                    if (onProgress) onProgress(s.progress || 50, s.message || 'Processing...');
                    if (s.status === 'completed') { clearInterval(iv); resolve(s); }
                    else if (s.status === 'cancelled') { clearInterval(iv); reject(new Error('cancelled')); }
                    else if (s.status === 'error') { clearInterval(iv); reject(new Error(s.message || 'Server error')); }
                } catch {
                    clearInterval(iv);
                    reject(new Error('Connection lost'));
                }
            }, 700);
        });
    }

    function bindDropzone(zoneSel, inputSel, onFile) {
        const zone = $(zoneSel);
        const input = $(inputSel);
        zone.addEventListener('click', () => input.click());
        zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('drag-over'); });
        zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
        zone.addEventListener('drop', (e) => {
            e.preventDefault();
            zone.classList.remove('drag-over');
            if (e.dataTransfer.files.length) onFile(e.dataTransfer.files[0]);
        });
        input.addEventListener('change', () => {
            if (input.files.length) onFile(input.files[0]);
        });
    }

    function isValidAudio(file) {
        return (file.type.startsWith('audio/') || file.type === 'video/mp4'
            || file.name.match(/\.(mp3|wav|flac|m4a|aac|ogg|opus|wma|aiff?|mp4)$/i))
            && file.size <= 500 * 1024 * 1024;
    }

    function fmtDur(sec) {
        const m = Math.floor(sec / 60), s = Math.round(sec % 60);
        return `${m}:${String(s).padStart(2, '0')}`;
    }

    function fmtSize(bytes) {
        if (bytes > 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        return (bytes / 1024).toFixed(0) + ' KB';
    }

    // ========================= VOCAL REMOVER =========================

    const sep = { file: null, taskId: null, stems: null, processing: false, cancelRequested: false };

    function initSep() {
        bindDropzone('#sep-dropzone', '#sep-file-input', (file) => {
            if (!isValidAudio(file)) { showToast('Please select an audio file under 500MB.'); return; }
            sep.file = file;
            sep.taskId = null;
            sep.stems = null;
            $('#sep-file-name').textContent = file.name;
            $('#sep-file-info').textContent = fmtSize(file.size);
            $('#sep-upload-section').classList.add('hidden');
            $('#sep-app-section').classList.remove('hidden');
            $('#sep-new-row').classList.remove('hidden');
            $('#sep-results').classList.add('hidden');
            $('#sep-start-btn').disabled = false;
        });

        $('#sep-start-btn').addEventListener('click', sepStart);
        $('#sep-cancel-btn').addEventListener('click', () => {
            sep.cancelRequested = true;
            if (sep.taskId) fetch(`/api/image/cancel/${sep.taskId}`, { method: 'POST' }).catch(() => {});
        });
        $('#sep-new-btn').addEventListener('click', () => {
            $('#sep-app-section').classList.add('hidden');
            $('#sep-new-row').classList.add('hidden');
            $('#sep-upload-section').classList.remove('hidden');
            $('#sep-file-input').value = '';
        });
        $('#sep-vocals-save').addEventListener('click', () => {
            if (sep.stems) saveServerResult(sep.stems.vocals, stemName('vocals'));
        });
        $('#sep-inst-save').addEventListener('click', () => {
            if (sep.stems) saveServerResult(sep.stems.instrumental, stemName('instrumental'));
        });
        $('#sep-zip-btn').addEventListener('click', sepZip);
    }

    function stemName(kind) {
        const stem = sep.file ? sep.file.name.replace(/\.[^.]+$/, '') : 'song';
        return `${stem}_${kind}.wav`;
    }

    function sepProgress(pct, msg) {
        $('#sep-progress-fill').style.width = pct + '%';
        $('#sep-progress-percent').textContent = pct + '%';
        $('#sep-progress-message').textContent = msg;
    }

    async function sepStart() {
        if (sep.processing || !sep.file) return;
        sep.processing = true;
        sep.cancelRequested = false;
        $('#sep-start-btn').disabled = true;
        $('#sep-progress-section').classList.remove('hidden');
        $('#sep-results').classList.add('hidden');
        $('#sep-new-row').classList.add('hidden');

        try {
            await ensureModel('separate', sepProgress);
            sepProgress(2, 'Uploading audio...');
            const up = await uploadAudio(sep.file);
            sep.taskId = up.task_id;
            $('#sep-file-info').textContent = `${fmtSize(sep.file.size)} • ${fmtDur(up.duration)}`;
            const res = await fetch('/api/audio/separate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ task_id: up.task_id }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.error || 'Failed to start');
            }
            await pollStatus(up.task_id, sepProgress, () => sep.cancelRequested);
            sep.stems = { vocals: up.task_id + '_voc', instrumental: up.task_id + '_ins' };
            $('#sep-vocals-audio').src = `/api/audio/download/${sep.stems.vocals}`;
            $('#sep-inst-audio').src = `/api/audio/download/${sep.stems.instrumental}`;
            $('#sep-results').classList.remove('hidden');
        } catch (err) {
            if (sep.cancelRequested || (err && err.message === 'cancelled')) {
                showToast('Cancelled.');
            } else {
                showToast('Separation failed: ' + err.message);
            }
        } finally {
            sep.processing = false;
            $('#sep-progress-section').classList.add('hidden');
            $('#sep-new-row').classList.remove('hidden');
            $('#sep-start-btn').disabled = false;
        }
    }

    async function sepZip() {
        if (!sep.stems) return;
        const btn = $('#sep-zip-btn');
        btn.disabled = true;
        btn.textContent = 'Zipping...';
        try {
            const res = await fetch('/api/zip', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ task_ids: [sep.stems.vocals, sep.stems.instrumental] }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || 'Zip failed');
            const stem = sep.file ? sep.file.name.replace(/\.[^.]+$/, '') : 'stems';
            const dlg = desktopDialog();
            if (dlg) {
                await saveServerResult(data.zip_id, `${stem}_stems.zip`);
            } else if (!await browserSaveAs(`${stem}_stems.zip`, async () => {
                const r = await fetch(`/api/zip/${data.zip_id}`);
                if (!r.ok) throw new Error('download failed');
                return r.blob();
            })) {
                const a = document.createElement('a');
                a.href = `/api/zip/${data.zip_id}`;
                a.download = `${stem}_stems.zip`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
            }
        } catch (e) {
            showToast('Zip failed: ' + e.message);
        } finally {
            btn.disabled = false;
            btn.textContent = 'Download Both (.zip)';
        }
    }

    // ========================= NOISE REMOVER =========================

    const aden = { file: null, taskId: null, processing: false, cancelRequested: false, originalUrl: null };

    function initAdenoise() {
        bindDropzone('#adenoise-dropzone', '#adenoise-file-input', (file) => {
            if (!isValidAudio(file)) { showToast('Please select an audio file under 500MB.'); return; }
            aden.file = file;
            aden.taskId = null;
            if (aden.originalUrl) URL.revokeObjectURL(aden.originalUrl);
            aden.originalUrl = URL.createObjectURL(file);
            $('#adenoise-file-name').textContent = file.name;
            $('#adenoise-file-info').textContent = fmtSize(file.size);
            $('#adenoise-upload-section').classList.add('hidden');
            $('#adenoise-app-section').classList.remove('hidden');
            $('#adenoise-new-row').classList.remove('hidden');
            $('#adenoise-results').classList.add('hidden');
            $('#adenoise-start-btn').disabled = false;
        });

        $('#adenoise-start-btn').addEventListener('click', adenStart);
        $('#adenoise-cancel-btn').addEventListener('click', () => {
            aden.cancelRequested = true;
            if (aden.taskId) fetch(`/api/image/cancel/${aden.taskId}`, { method: 'POST' }).catch(() => {});
        });
        $('#adenoise-new-btn').addEventListener('click', () => {
            $('#adenoise-app-section').classList.add('hidden');
            $('#adenoise-new-row').classList.add('hidden');
            $('#adenoise-upload-section').classList.remove('hidden');
            $('#adenoise-file-input').value = '';
        });
        $('#adenoise-save').addEventListener('click', () => {
            if (!aden.taskId) return;
            const stem = aden.file ? aden.file.name.replace(/\.[^.]+$/, '') : 'audio';
            saveServerResult(aden.taskId, `${stem}_denoised.wav`);
        });
    }

    function adenProgress(pct, msg) {
        $('#adenoise-progress-fill').style.width = pct + '%';
        $('#adenoise-progress-percent').textContent = pct + '%';
        $('#adenoise-progress-message').textContent = msg;
    }

    async function adenStart() {
        if (aden.processing || !aden.file) return;
        aden.processing = true;
        aden.cancelRequested = false;
        $('#adenoise-start-btn').disabled = true;
        $('#adenoise-progress-section').classList.remove('hidden');
        $('#adenoise-results').classList.add('hidden');
        $('#adenoise-new-row').classList.add('hidden');

        try {
            await ensureModel('denoise_speech', adenProgress);
            adenProgress(2, 'Uploading audio...');
            const up = await uploadAudio(aden.file);
            aden.taskId = up.task_id;
            $('#adenoise-file-info').textContent = `${fmtSize(aden.file.size)} • ${fmtDur(up.duration)}`;
            const res = await fetch('/api/audio/denoise', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ task_id: up.task_id }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.error || 'Failed to start');
            }
            await pollStatus(up.task_id, adenProgress, () => aden.cancelRequested);
            $('#adenoise-before-audio').src = aden.originalUrl;
            $('#adenoise-after-audio').src = `/api/audio/download/${up.task_id}?t=${Date.now()}`;
            $('#adenoise-results').classList.remove('hidden');
        } catch (err) {
            if (aden.cancelRequested || (err && err.message === 'cancelled')) {
                showToast('Cancelled.');
            } else {
                showToast('Denoise failed: ' + err.message);
            }
        } finally {
            aden.processing = false;
            $('#adenoise-progress-section').classList.add('hidden');
            $('#adenoise-new-row').classList.remove('hidden');
            $('#adenoise-start-btn').disabled = false;
        }
    }

    // ========================= MASTERING (in-browser, ReMaster core) =========================

    const master = {
        file: null, worker: null, nextId: 1, audio: null,
        resultBlob: null, processing: false,
    };

    function masterWorker() {
        if (!master.worker) master.worker = new Worker('/static/js/vendor/remaster-worker.js');
        return master.worker;
    }

    function workerCall(kind, payload, onProgress, transfer) {
        return new Promise((resolve, reject) => {
            const id = master.nextId++;
            const w = masterWorker();
            const handler = (e) => {
                const m = e.data;
                if (m.id !== id) return;
                if (m.kind === 'progress') { if (onProgress) onProgress(m.stage, m.fraction); return; }
                w.removeEventListener('message', handler);
                if (m.kind === 'error') reject(new Error(m.message));
                else resolve(m);
            };
            w.addEventListener('message', handler);
            w.postMessage({ id, kind, ...payload }, transfer || []);
        });
    }

    function initMaster() {
        bindDropzone('#master-dropzone', '#master-file-input', async (file) => {
            if (!isValidAudio(file)) { showToast('Please select an audio file.'); return; }
            master.file = file;
            master.resultBlob = null;
            $('#master-file-name').textContent = file.name;
            $('#master-file-info').textContent = fmtSize(file.size) + ' • decoding...';
            $('#master-upload-section').classList.add('hidden');
            $('#master-app-section').classList.remove('hidden');
            $('#master-new-row').classList.remove('hidden');
            $('#master-results').classList.add('hidden');
            $('#master-start-btn').disabled = true;
            try {
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                const buf = await ctx.decodeAudioData(await file.arrayBuffer());
                ctx.close();
                const channels = [];
                for (let c = 0; c < Math.min(2, buf.numberOfChannels); c++) {
                    channels.push(buf.getChannelData(c).slice());
                }
                master.audio = { sampleRate: buf.sampleRate, channels };
                $('#master-file-info').textContent =
                    `${fmtSize(file.size)} • ${fmtDur(buf.duration)} • ${buf.sampleRate} Hz • runs in this window, nothing is uploaded`;
                $('#master-start-btn').disabled = false;
            } catch (e) {
                showToast('Could not decode this audio file.');
                $('#master-new-btn').click();
            }
        });

        $('#master-start-btn').addEventListener('click', masterStart);
        $('#master-new-btn').addEventListener('click', () => {
            $('#master-app-section').classList.add('hidden');
            $('#master-new-row').classList.add('hidden');
            $('#master-upload-section').classList.remove('hidden');
            $('#master-file-input').value = '';
            master.audio = null;
        });
        $('#master-save').addEventListener('click', () => {
            if (!master.resultBlob) return;
            const stem = master.file ? master.file.name.replace(/\.[^.]+$/, '') : 'track';
            saveBlobResult(master.resultBlob, `${stem}_mastered.wav`);
        });
    }

    function masterProgress(pct, msg) {
        $('#master-progress-fill').style.width = pct + '%';
        $('#master-progress-percent').textContent = pct + '%';
        $('#master-progress-message').textContent = msg;
    }

    async function masterStart() {
        if (master.processing || !master.audio) return;
        master.processing = true;
        $('#master-start-btn').disabled = true;
        $('#master-progress-section').classList.remove('hidden');
        $('#master-results').classList.add('hidden');

        try {
            masterProgress(5, 'Measuring the track...');
            const analysed = await workerCall('analyse', { audio: master.audio });
            const before = analysed.analysis;
            const mastered = await workerCall('master',
                { audio: master.audio, analysis: before },
                (stage, fraction) => masterProgress(10 + Math.round(fraction * 85), stage));
            const after = mastered.after;

            const wav = window.RemasterLib.encodeWav24(mastered.result.audio);
            master.resultBlob = new Blob([wav], { type: 'audio/wav' });
            $('#master-audio').src = URL.createObjectURL(master.resultBlob);

            $('#master-stat-lufs').textContent =
                `${before.lufs.toFixed(1)} → ${after.lufs.toFixed(1)} LUFS`;
            $('#master-stat-peak').textContent =
                `${before.truePeak.toFixed(1)} → ${after.truePeak.toFixed(1)} dBTP`;
            $('#master-stat-lra').textContent =
                `${before.lra.toFixed(1)} → ${after.lra.toFixed(1)} LU`;
            $('#master-stat-target').textContent =
                `${mastered.plan.targetLufs} LUFS (${mastered.plan.curveName})`;
            const list = $('#master-why-list');
            list.innerHTML = '';
            for (const why of mastered.plan.why) {
                const li = document.createElement('li');
                li.textContent = why;
                list.appendChild(li);
            }
            if (mastered.result.backedOff > 0) {
                const li = document.createElement('li');
                li.textContent = `backed off ${mastered.result.backedOff.toFixed(1)} dB to avoid audible clipping`;
                list.appendChild(li);
            }
            // The mastered buffer was transferred to the worker result; re-decode
            // is not needed, but the source AudioData is now detached — reload it
            // if the user masters again.
            master.audio = null;
            $('#master-start-btn').disabled = true;
            $('#master-results').classList.remove('hidden');
        } catch (err) {
            showToast('Mastering failed: ' + err.message);
            $('#master-start-btn').disabled = false;
        } finally {
            master.processing = false;
            $('#master-progress-section').classList.add('hidden');
        }
    }

    // ========================= TAG EDITOR (in-browser, ReMaster core) =========================

    const tags = { file: null, bytes: null, resultBlob: null, resultExt: '' };

    function initTags() {
        bindDropzone('#tags-dropzone', '#tags-file-input', async (file) => {
            if (!file.name.match(/\.(mp3|wav)$/i)) {
                showToast('The Tag Editor supports MP3 and WAV files.');
                return;
            }
            tags.file = file;
            tags.resultBlob = null;
            tags.bytes = new Uint8Array(await file.arrayBuffer());
            $('#tags-file-name').textContent = file.name;
            $('#tags-file-info').textContent = fmtSize(file.size);
            $('#tags-upload-section').classList.add('hidden');
            $('#tags-app-section').classList.remove('hidden');
            $('#tags-new-row').classList.remove('hidden');
            $('#tags-results').classList.add('hidden');
            $('#tags-clean-btn').disabled = false;
            // Show current tags without modifying anything: run a strip in
            // memory purely to read the before-table
            try {
                const r = await workerCall('tags', { bytes: tags.bytes, mode: 'strip', tags: {} });
                renderTagTable($('#tags-before-table'), r.result.tagsBefore);
            } catch { /* table stays empty */ }
        });

        $('#tags-clean-btn').addEventListener('click', tagsClean);
        $('#tags-new-btn').addEventListener('click', () => {
            $('#tags-app-section').classList.add('hidden');
            $('#tags-new-row').classList.add('hidden');
            $('#tags-upload-section').classList.remove('hidden');
            $('#tags-file-input').value = '';
            tags.bytes = null;
        });
        $('#tags-save').addEventListener('click', () => {
            if (!tags.resultBlob) return;
            const stem = tags.file ? tags.file.name.replace(/\.[^.]+$/, '') : 'audio';
            saveBlobResult(tags.resultBlob, `${stem}_clean${tags.resultExt}`);
        });
    }

    function renderTagTable(table, rows) {
        table.innerHTML = '';
        if (!rows || !rows.length) {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.colSpan = 2;
            td.textContent = 'No tags found — this file is already clean.';
            tr.appendChild(td);
            table.appendChild(tr);
            return;
        }
        for (const row of rows) {
            const tr = document.createElement('tr');
            const k = document.createElement('td');
            k.textContent = row.label;
            const v = document.createElement('td');
            v.textContent = row.value;
            tr.append(k, v);
            table.appendChild(tr);
        }
    }

    async function tagsClean() {
        if (!tags.bytes) return;
        $('#tags-clean-btn').disabled = true;
        try {
            const r = await workerCall('tags', { bytes: tags.bytes, mode: 'strip', tags: {} });
            const res = r.result;
            if (res.error) throw new Error(res.error);
            tags.resultBlob = new Blob([res.bytes], { type: res.mime || 'application/octet-stream' });
            tags.resultExt = res.ext ? '.' + res.ext.replace(/^\./, '') : '';
            const removed = res.front + res.back;
            $('#tags-result-details').textContent = removed
                ? `Removed ${res.names.join(', ') || 'metadata'} (${(removed / 1024).toFixed(1)} KB). ` +
                  (res.audioIdentical ? 'Audio verified bit-for-bit identical.' : 'Warning: audio bytes changed.')
                : 'No tag blocks to remove — file saved as-is.';
            $('#tags-results').classList.remove('hidden');
        } catch (e) {
            showToast('Tag cleanup failed: ' + e.message);
        } finally {
            $('#tags-clean-btn').disabled = false;
        }
    }

    // The mastering worker doubles as the tag worker
    initSep();
    initAdenoise();
    initMaster();
    initTags();
})();
