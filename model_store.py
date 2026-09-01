"""Registry and download manager for the optional AI models.

Models are too large to bundle in the installer, so each downloads on
first use into the writable data dir (the same drop-in folder GFPGAN
uses, so offline users can place files manually). Some models ship as
two files (ONNX graph + external weight data) — the registry lists every
file a model needs, and progress aggregates across them.
"""

import os
import threading

import paths

HF = 'https://huggingface.co'

# key -> {label, files: [(url, filename, approx_bytes)]}
MODELS = {
    'bg': {
        'label': 'BiRefNet background removal',
        'files': [(f'{HF}/onnx-community/BiRefNet_lite/resolve/main/onnx/model.onnx',
                   'birefnet-lite.onnx', 224005088)],
    },
    'denoise': {
        'label': 'SCUNet denoise',
        'files': [(f'{HF}/Heliosoph/scunet-onnx/resolve/main/scunet_color_real_psnr.onnx',
                   'scunet_color_real_psnr.onnx', 3798678),
                  (f'{HF}/Heliosoph/scunet-onnx/resolve/main/scunet_color_real_psnr.onnx.data',
                   'scunet_color_real_psnr.onnx.data', 73134336)],
    },
    'deblur': {
        'label': 'NAFNet deblur',
        'files': [(f'{HF}/opencv/deblurring_nafnet/resolve/main/deblurring_nafnet_2025may.onnx',
                   'nafnet-deblur.onnx', 91736251)],
    },
    'colorize': {
        'label': 'DDColor colorization',
        'files': [(f'{HF}/edgetools/ddcolor/resolve/main/ddcolor-tiny-fp16.onnx',
                   'ddcolor-tiny.onnx', 135399424)],
    },
    'erase': {
        'label': 'LaMa object eraser',
        'files': [(f'{HF}/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx',
                   'lama.onnx', 207971136)],
    },
}

_lock = threading.Lock()
# key -> {'status': idle|downloading|done|error, 'progress': int, 'error': str|None}
_state = {k: {'status': 'idle', 'progress': 0, 'error': None} for k in MODELS}


def valid(key):
    return key in MODELS


def model_path(key, index=0):
    """Path of one of the model's files if present (data dir first)."""
    name = MODELS[key]['files'][index][1]
    return paths.find_model(name)


def available(key):
    return all(paths.find_model(f[1]) is not None for f in MODELS[key]['files'])


def total_size(key):
    return sum(f[2] for f in MODELS[key]['files'])


def status(key):
    with _lock:
        state = dict(_state[key])
    state['available'] = available(key)
    state['size'] = total_size(key)
    return state


def start_download(key):
    """Kick off the model download in a background thread (idempotent)."""
    with _lock:
        if _state[key]['status'] == 'downloading' or available(key):
            return
        _state[key].update(status='downloading', progress=0, error=None)
    threading.Thread(target=_download, args=(key,), daemon=True).start()


def _download(key):
    import ssl
    import urllib.request
    dest_dir = paths.data('models')
    os.makedirs(dest_dir, exist_ok=True)
    total = total_size(key)
    done_before = 0
    try:
        ctx = None
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
        for url, name, approx in MODELS[key]['files']:
            dest = os.path.join(dest_dir, name)
            if os.path.exists(dest):
                done_before += approx
                continue
            part = dest + '.part'
            req = urllib.request.Request(url, headers={'User-Agent': 'NextGenUp'})
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                file_total = int(resp.headers.get('Content-Length') or approx)
                done = 0
                with open(part, 'wb') as f:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        with _lock:
                            _state[key]['progress'] = min(
                                99, int((done_before + done) / total * 100))
            if os.path.getsize(part) < file_total * 0.98:
                raise OSError(f'Download of {name} incomplete')
            os.replace(part, dest)
            done_before += approx
        with _lock:
            _state[key].update(status='done', progress=100)
        print(f'[models] {key} downloaded to {dest_dir}', flush=True)
    except Exception as e:
        with _lock:
            _state[key].update(status='error', error=str(e)[:200])
        print(f'[models] {key} download failed: {e}', flush=True)
