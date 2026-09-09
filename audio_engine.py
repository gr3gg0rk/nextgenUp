"""Server-side audio tools via ONNX Runtime (CPU):

- separate: MDX-Net (Kim_Vocal_2) vocal/instrumental stem separation, 44.1kHz
- denoise:  DeepFilterNet3 speech enhancement, 48kHz

Audio IO goes through the bundled ffmpeg (any input format ffmpeg reads).
Models download on first use — see model_store.py.

The DFN3 implementation follows the DSP contract published with the
soniqo/DeepFilterNet3-ONNX export and libDF v0.5.6: vorbis-window STFT with
wnorm analysis scaling, ERB mean-norm and complex unit-norm features
(alpha 0.99), ERB mask + order-5 deep filtering. The one-frame output
offset and mask alignment below were validated empirically (SI-SDR
+5 dB on a 10 dB white-noise test; wrong offsets score negative).
"""

import os
import subprocess
import sys
import threading

import numpy as np

import model_store
import paths

_sessions = {}
_session_lock = threading.Lock()
_infer_lock = threading.Lock()


def _ffmpeg():
    exe = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
    if paths.EXE_DIR:
        p = os.path.join(paths.EXE_DIR, exe)
        if os.path.isfile(p):
            return p
    import shutil
    return shutil.which('ffmpeg') or 'ffmpeg'


def _get_session(key):
    import onnxruntime as ort
    with _session_lock:
        if key not in _sessions:
            ort.set_default_logger_severity(4)
            _sessions[key] = ort.InferenceSession(
                model_store.model_path(key), providers=['CPUExecutionProvider']
            )
        return _sessions[key]


def load_audio(path, sr, channels):
    """Decode any ffmpeg-readable file to float32 (channels, n)."""
    raw = subprocess.run(
        [_ffmpeg(), '-v', 'error', '-i', path, '-f', 'f32le',
         '-ac', str(channels), '-ar', str(sr), '-'],
        capture_output=True)
    if raw.returncode != 0 or not raw.stdout:
        err = raw.stderr.decode(errors='replace').strip().splitlines()
        raise ValueError(err[-1][:200] if err else 'Could not decode audio')
    data = np.frombuffer(raw.stdout, np.float32)
    return data.reshape(-1, channels).T.copy()


def save_audio(path, data, sr):
    """Encode float32 (channels, n) to the format implied by the extension."""
    if data.ndim == 1:
        data = data[np.newaxis]
    subprocess.run(
        [_ffmpeg(), '-y', '-v', 'error', '-f', 'f32le',
         '-ac', str(data.shape[0]), '-ar', str(sr), '-i', '-', path],
        input=np.ascontiguousarray(data.T, np.float32).tobytes(), check=True)


def probe_duration(path):
    """Duration in seconds via ffmpeg (works without ffprobe)."""
    r = subprocess.run([_ffmpeg(), '-hide_banner', '-i', path],
                       capture_output=True, text=True, timeout=30)
    import re
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', r.stderr)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


# --- Vocal separation (MDX-Net, Kim_Vocal_2) ---

MDX_SR = 44100
MDX_NFFT = 7680
MDX_HOP = 1024
MDX_DIM_F = 3072
MDX_DIM_T = 256
MDX_SEG = (MDX_DIM_T - 1) * MDX_HOP        # samples per model window (~5.9s)
MDX_OVERLAP = MDX_SR                        # 1s crossfade between windows

_mdx_window = np.hanning(MDX_NFFT + 1)[:-1].astype(np.float32)


def _mdx_stft(x):
    pad = MDX_NFFT // 2
    xp = np.pad(x, ((0, 0), (pad, pad)), mode='reflect')
    frames = np.lib.stride_tricks.sliding_window_view(
        xp, MDX_NFFT, axis=1)[:, ::MDX_HOP]
    return np.fft.rfft(frames[:, :MDX_DIM_T] * _mdx_window,
                       axis=-1).transpose(0, 2, 1)


def _mdx_istft(spec):
    frames = np.fft.irfft(spec.transpose(0, 2, 1), n=MDX_NFFT,
                          axis=-1) * _mdx_window
    T = spec.shape[2]
    length = (T - 1) * MDX_HOP + MDX_NFFT
    out = np.zeros((2, length), np.float32)
    norm = np.zeros(length, np.float32)
    wsq = _mdx_window ** 2
    for t in range(T):
        out[:, t * MDX_HOP:t * MDX_HOP + MDX_NFFT] += frames[:, t]
        norm[t * MDX_HOP:t * MDX_HOP + MDX_NFFT] += wsq
    out /= np.maximum(norm, 1e-8)
    pad = MDX_NFFT // 2
    return out[:, pad:pad + MDX_SEG]


def separate(in_path, vocals_path, inst_path, progress_cb=None):
    """Split a song into vocals + instrumental. Returns duration seconds."""
    sess = _get_session('separate')
    input_name = sess.get_inputs()[0].name
    mix = load_audio(in_path, MDX_SR, 2)
    n = mix.shape[1]

    vocal = np.zeros_like(mix)
    weight = np.zeros(n, np.float32)
    step = MDX_SEG - MDX_OVERLAP
    positions = list(range(0, n, step))
    for idx, pos in enumerate(positions):
        seg = mix[:, pos:pos + MDX_SEG]
        L = seg.shape[1]
        if L < MDX_SEG:
            seg = np.pad(seg, ((0, 0), (0, MDX_SEG - L)))
        spec = _mdx_stft(seg)
        x = np.stack([spec.real[:, :MDX_DIM_F], spec.imag[:, :MDX_DIM_F]],
                     1).reshape(4, MDX_DIM_F, MDX_DIM_T).astype(np.float32)
        with _infer_lock:
            out = sess.run(None, {input_name: x[np.newaxis]})[0][0]
        vspec = np.zeros_like(spec)
        vspec[:, :MDX_DIM_F] = out[[0, 2]] + 1j * out[[1, 3]]
        v = _mdx_istft(vspec)[:, :L]

        w = np.ones(L, np.float32)
        ramp = min(MDX_OVERLAP, L)
        if pos > 0:
            w[:ramp] = np.linspace(0, 1, ramp)
        if pos + MDX_SEG < n:
            w[-ramp:] = np.minimum(w[-ramp:], np.linspace(1, 0, ramp))
        vocal[:, pos:pos + L] += v * w
        weight[pos:pos + L] += w
        if progress_cb:
            progress_cb(idx + 1, len(positions))

    vocal /= np.maximum(weight, 1e-8)
    save_audio(vocals_path, vocal, MDX_SR)
    save_audio(inst_path, mix - vocal, MDX_SR)
    return n / MDX_SR


# --- Denoise (DeepFilterNet3) ---

DF_SR = 48000
DF_FFT = 960
DF_HOP = 480
DF_NB_ERB = 32
DF_NB_DF = 96
DF_ORDER = 5
DF_WNORM = 1.0 / (DF_FFT * DF_FFT / (2 * DF_HOP))
DF_ALPHA = 0.99
# Empirically validated output alignment (see module docstring)
DF_TAP_OFF = 1
DF_MASK_ROLL = 2
DF_CHUNK = 60 * DF_SR                        # process 60s at a time
DF_CHUNK_OVERLAP = DF_SR                     # 1s crossfade

_df_aux = None


def _df_matrices():
    global _df_aux
    if _df_aux is None:
        aux_path = paths.find_model('deepfilter3-auxiliary.bin')
        aux = np.fromfile(aux_path, np.float32)
        erb_fb = aux[:481 * 32].reshape(481, 32)
        erb_inv = aux[481 * 32:481 * 32 + 32 * 481].reshape(32, 481)
        window = aux[481 * 32 + 32 * 481:]
        _df_aux = (erb_fb, erb_inv, window)
    return _df_aux


def _denoise_chunk(sess, sig, erb_fb, erb_inv, window):
    """Denoise one mono float32 chunk at 48k."""
    x = np.concatenate([np.zeros(DF_HOP, np.float32), sig,
                        np.zeros(DF_FFT, np.float32)])
    nfr = (len(x) - DF_FFT) // DF_HOP + 1
    frames = np.lib.stride_tricks.sliding_window_view(x, DF_FFT)[::DF_HOP][:nfr]
    spec = np.fft.rfft(frames * window, axis=-1) * DF_WNORM

    erb_db = 10 * np.log10((np.abs(spec) ** 2) @ erb_fb + 1e-10)
    m = np.linspace(-60, -90, DF_NB_ERB)
    fe = np.empty_like(erb_db)
    for t in range(nfr):
        m = DF_ALPHA * m + (1 - DF_ALPHA) * erb_db[t]
        fe[t] = (erb_db[t] - m) / 40.0
    s = np.linspace(1e-3, 1e-4, DF_NB_DF)
    fs = np.empty((nfr, DF_NB_DF), np.complex128)
    for t in range(nfr):
        s = DF_ALPHA * s + (1 - DF_ALPHA) * np.abs(spec[t, :DF_NB_DF])
        fs[t] = spec[t, :DF_NB_DF] / np.sqrt(s)

    with _infer_lock:
        mask, coefs = sess.run(None, {
            'feat_erb': fe[np.newaxis, np.newaxis].astype(np.float32),
            'feat_spec': np.stack([fs.real, fs.imag])[np.newaxis].astype(np.float32),
        })
    mask = mask[0, 0]
    c = coefs[0, :, :, :, 0] + 1j * coefs[0, :, :, :, 1]

    out = spec * (np.roll(mask, DF_MASK_ROLL, axis=0) @ erb_inv)
    padded = np.pad(spec[:, :DF_NB_DF], ((DF_ORDER - 1, DF_ORDER - 1), (0, 0)))
    df = np.zeros((nfr, DF_NB_DF), np.complex128)
    for i in range(DF_ORDER):
        df += c[i] * padded[i + DF_TAP_OFF:i + DF_TAP_OFF + nfr]
    out[:, :DF_NB_DF] = df

    fr = np.fft.irfft(out * DF_FFT, n=DF_FFT, axis=-1) * window
    y = np.zeros(len(x), np.float32)
    for t in range(nfr):
        y[t * DF_HOP:t * DF_HOP + DF_FFT] += fr[t]
    return y[2 * DF_HOP:2 * DF_HOP + len(sig)]


def denoise(in_path, out_path, progress_cb=None):
    """Remove background noise from speech. Returns duration seconds."""
    sess = _get_session('denoise_speech')
    erb_fb, erb_inv, window = _df_matrices()
    sig = load_audio(in_path, DF_SR, 1)[0]
    n = len(sig)

    out = np.zeros(n, np.float32)
    weight = np.zeros(n, np.float32)
    step = DF_CHUNK - DF_CHUNK_OVERLAP
    positions = list(range(0, n, step))
    for idx, pos in enumerate(positions):
        chunk = sig[pos:pos + DF_CHUNK]
        y = _denoise_chunk(sess, chunk, erb_fb, erb_inv, window)
        L = len(y)
        w = np.ones(L, np.float32)
        ramp = min(DF_CHUNK_OVERLAP, L)
        if pos > 0:
            w[:ramp] = np.linspace(0, 1, ramp)
        if pos + DF_CHUNK < n:
            w[-ramp:] = np.minimum(w[-ramp:], np.linspace(1, 0, ramp))
        out[pos:pos + L] += y * w
        weight[pos:pos + L] += w
        if progress_cb:
            progress_cb(idx + 1, len(positions))

    out /= np.maximum(weight, 1e-8)
    save_audio(out_path, out, DF_SR)
    return n / DF_SR
