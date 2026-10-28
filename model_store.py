"""Registry and download manager for the optional AI models.

The registry lives in ``registry.json`` in the writable data dir and is
seeded on first run from ``BUILTIN_MODELS``; user-added models persist.
Legacy models download single files (plus tar extraction for
DeepFilterNet) into ``models/`` so offline users can drop files in
manually; snapshot-style repos go into ``models/<key>/``.

Downloads run on background threads with a progress state machine.
Every install records the resolved HF revision + etag so "update" can
detect upstream changes. Registry writes are atomic.
"""

import copy
import json
import os
import shutil
import threading
import time

import paths

HF = 'https://huggingface.co'
HF_API = HF + '/api/models'

REGISTRY_VERSION = 1

CPU_KINDS = ('image', 'audio', 'image-bg')

# key -> registry entry (source of truth for seeding; the file may gain
# user entries but builtin definitions always win on reseed).
BUILTIN_MODELS = [
    {
        'key': 'bg',
        'label': 'BiRefNet background removal',
        'kind': 'image-bg',
        'engine': 'onnx',
        'source': {'type': 'files', 'revision': None, 'files': [
            {'url': f'{HF}/onnx-community/BiRefNet_lite/resolve/main/onnx/model.onnx',
             'name': 'birefnet-lite.onnx', 'bytes': 224005088},
        ]},
        'size_bytes': 224005088,
        'options': {},
    },
    {
        'key': 'denoise',
        'label': 'SCUNet denoise',
        'kind': 'image',
        'engine': 'onnx',
        'source': {'type': 'files', 'revision': None, 'files': [
            {'url': f'{HF}/Heliosoph/scunet-onnx/resolve/main/scunet_color_real_psnr.onnx',
             'name': 'scunet_color_real_psnr.onnx', 'bytes': 3798678},
            {'url': f'{HF}/Heliosoph/scunet-onnx/resolve/main/scunet_color_real_psnr.onnx.data',
             'name': 'scunet_color_real_psnr.onnx.data', 'bytes': 73134336},
        ]},
        'size_bytes': 76933014,
        'options': {},
    },
    {
        'key': 'deblur',
        'label': 'NAFNet deblur',
        'kind': 'image',
        'engine': 'onnx',
        'source': {'type': 'files', 'revision': None, 'files': [
            {'url': f'{HF}/opencv/deblurring_nafnet/resolve/main/deblurring_nafnet_2025may.onnx',
             'name': 'nafnet-deblur.onnx', 'bytes': 91736251},
        ]},
        'size_bytes': 91736251,
        'options': {},
    },
    {
        'key': 'colorize',
        'label': 'DDColor colorization',
        'kind': 'image',
        'engine': 'onnx',
        'source': {'type': 'files', 'revision': None, 'files': [
            {'url': f'{HF}/edgetools/ddcolor/resolve/main/ddcolor-tiny-fp16.onnx',
             'name': 'ddcolor-tiny.onnx', 'bytes': 135399424},
        ]},
        'size_bytes': 135399424,
        'options': {},
    },
    {
        'key': 'erase',
        'label': 'LaMa object eraser',
        'kind': 'image',
        'engine': 'onnx',
        'source': {'type': 'files', 'revision': None, 'files': [
            {'url': f'{HF}/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx',
             'name': 'lama.onnx', 'bytes': 207971136},
        ]},
        'size_bytes': 207971136,
        'options': {},
    },
    {
        'key': 'separate',
        'label': 'MDX-Net vocal separation (Kim Vocal 2)',
        'kind': 'audio',
        'engine': 'onnx',
        'source': {'type': 'files', 'revision': None, 'files': [
            {'url': f'{HF}/seanghay/uvr_models/resolve/main/Kim_Vocal_2.onnx',
             'name': 'kim_vocal_2.onnx', 'bytes': 66759214},
        ]},
        'size_bytes': 66759214,
        'options': {},
    },
    {
        'key': 'denoise_speech',
        'label': 'DeepFilterNet3 noise removal',
        # Official export from the DeepFilterNet repo (MIT): a tarball
        # with the encoder + two decoders, unpacked to the extracted names
        'kind': 'audio',
        'engine': 'onnx',
        'source': {'type': 'files', 'revision': None, 'files': [
            {'url': 'https://github.com/Rikorose/DeepFilterNet/raw/v0.5.6/models/DeepFilterNet3_onnx.tar.gz',
             'name': 'DeepFilterNet3_onnx.tar.gz', 'bytes': 7983136},
        ], 'extract': {'tmp/export/enc.onnx': 'dfn3_enc.onnx',
                       'tmp/export/erb_dec.onnx': 'dfn3_erb_dec.onnx',
                       'tmp/export/df_dec.onnx': 'dfn3_df_dec.onnx'}},
        'size_bytes': 7983136,
        'options': {},
    },
    # --- GPU tier (contract §6) ---
    {
        'key': 'realesrgan-x4v3-gpu',
        'label': 'Real-ESRGAN General x4v3 (GPU)',
        'kind': 'image-sr',
        'engine': 'onnx-gpu',
        'scale': 4,
        # The HF repo carries no weights; release_assets.json points at a
        # QAIRT zip containing real_esrgan_general_x4v3.onnx (+ external
        # .data), extracted flat into models/ so find_model sees it.
        'source': {
            'type': 'url',
            'repo': 'qualcomm/Real-ESRGAN-General-x4v3',
            'file': 'real_esrgan_general_x4v3-onnx-float.zip',
            'revision': 'main',
            'url': 'https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/real_esrgan_general_x4v3/releases/v0.65.0/real_esrgan_general_x4v3-onnx-float.zip',
            'extract': {'real_esrgan_general_x4v3-onnx-float/real_esrgan_general_x4v3.onnx': 'real_esrgan_general_x4v3.onnx',
                        'real_esrgan_general_x4v3-onnx-float/real_esrgan_general_x4v3.data': 'real_esrgan_general_x4v3.data'},
        },
        'size_bytes': 4540895,
        'options': {'tile': 0, 'dtype': 'fp16'},
    },
    {
        'key': 'swin2sr-x4',
        'label': 'Swin2SR Real-World x4',
        'kind': 'image-sr',
        'engine': 'torch-swin2sr',
        'scale': 4,
        'source': {
            'type': 'hf',
            'repo': 'caidas/swin2SR-realworld-sr-x4-64-bsrgan-psnr',
            'file': None,   # snapshot download
            'revision': 'main',
            'snapshot_files': ['config.json', 'preprocessor_config.json',
                               'pytorch_model.bin'],
        },
        'size_bytes': 48461065,
        'options': {'tile': 512, 'dtype': 'fp16'},
    },
    {
        'key': 'seedvr2-3b',
        'label': 'SeedVR2-3B (FP8, video restore 2x/4x)',
        'kind': 'video-sr',
        'engine': 'seedvr2',
        'scale': 4,   # restoration model; target size derives from input
        # numz wrapper (Apache-2.0, ByteDance-endorsed): fp8/fp16
        # safetensors that fit 12GB VRAM; the official fp32 .pth (12.6GB)
        # does not
        'source': {
            'type': 'hf',
            'repo': 'numz/SeedVR2_comfyUI',
            'file': None,
            'revision': '09ced71023636e9bc8cdf9cdecfb2625d1e691e8',
            'snapshot_files': ['seedvr2_ema_3b_fp8_e4m3fn.safetensors',
                               'ema_vae_fp16.safetensors',
                               'pos_emb.pt', 'neg_emb.pt'],
        },
        'size_bytes': 3894821150,
        'options': {'dit_model': 'seedvr2_ema_3b_fp8_e4m3fn.safetensors',
                    'batch_size': 5, 'attention_mode': 'sdpa',
                    'color_correction': 'lab'},
        'license': 'Apache-2.0',
    },
    {
        'key': 'ltxv-upscaler',
        'label': 'LTX spatial upscaler 0.9.7',
        'kind': 'video-sr',
        'engine': 'ltx-upscaler',
        'source': {
            'type': 'hf',
            'repo': 'Lightricks/ltxv-spatial-upscaler-0.9.7',
            'file': None,
            'revision': 'main',
            # latent_upsampler only — the shared VAE ships with the LTX
            # engine env, not with this model
            'snapshot_files': ['latent_upsampler/config.json',
                               'latent_upsampler/diffusion_pytorch_model.safetensors',
                               'model_index.json'],
        },
        'size_bytes': 505010137,
        'options': {'dtype': 'bf16'},
    },
]

_reg_lock = threading.RLock()
_lock = threading.RLock()

# key -> {'status': idle|downloading|done|error, 'progress': int, 'error': str|None}
_state = {}


def registry_path():
    return paths.data('registry.json')


def _seed():
    """Build a fresh registry document from BUILTIN_MODELS."""
    return {
        'version': REGISTRY_VERSION,
        'models': [copy.deepcopy(m) for m in BUILTIN_MODELS],
    }


def load_registry():
    """Read registry.json, seeding it from builtins on first run."""
    with _reg_lock:
        path = registry_path()
        doc = None
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    doc = json.load(f)
            except (OSError, ValueError) as e:
                print(f'[registry] corrupt, reseeding: {e}', flush=True)
                doc = None
        if not doc or not isinstance(doc.get('models'), list):
            doc = _seed()
            _write_registry(doc)
        return doc


def _write_registry(doc):
    """Atomically persist the registry document."""
    path = registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _entries():
    return load_registry()['models']


def get(key):
    for m in _entries():
        if m['key'] == key:
            return m
    return None


def list_models():
    """All registry entries (deep copies — callers may mutate)."""
    return copy.deepcopy(_entries())


def valid(key):
    return get(key) is not None


def is_gpu(key):
    m = get(key)
    return bool(m) and m.get('engine') not in (None, 'onnx')


def _state_for(key):
    with _lock:
        if key not in _state:
            _state[key] = {'status': 'idle', 'progress': 0, 'error': None}
        return _state[key]


# --- File layout helpers ---

def _model_dir(key):
    """Cache dir for snapshot-style repos."""
    return paths.data(os.path.join('models', key))


def _flat_names(entry):
    """Filenames this model lands under models/ (legacy + single-file)."""
    src = entry.get('source', {})
    if src.get('type') == 'files':
        if 'extract' in src:
            return list(src['extract'].values())
        return [f['name'] for f in src['files']]
    if src.get('type') == 'url':
        return list(src.get('extract', {}).values())
    return []


def _snapshot_files(entry):
    src = entry.get('source', {})
    files = src.get('snapshot_files')
    if files is None and src.get('file'):
        files = [src['file']]
    return files or []


def model_path(key, index=0):
    """Path of one of the model's files if present (data dir first)."""
    entry = get(key)
    if entry is None:
        return None
    if entry['source'].get('type') == 'hf' and not entry['source'].get('file'):
        present = _present_snapshot(entry, _snapshot_files(entry))
        return present[index] if index < len(present) else None
    names = _flat_names(entry)
    if index >= len(names):
        return None
    return paths.find_model(names[index])


def _present_snapshot(entry, files):
    """Absolute paths of snapshot files that exist under models/<key>/."""
    root = _model_dir(entry['key'])
    out = []
    for rel in files:
        p = os.path.join(root, rel.replace('/', os.sep))
        if os.path.exists(p):
            out.append(p)
    return out


def available(key):
    entry = get(key)
    if entry is None:
        return False
    snap = _snapshot_files(entry)
    if snap and entry['source'].get('type') == 'hf':
        return len(_present_snapshot(entry, snap)) == len(snap)
    return all(paths.find_model(n) is not None for n in _flat_names(entry))


def total_size(key):
    entry = get(key)
    if entry is None:
        return 0
    return int(entry.get('size_bytes') or 0)


def status(key):
    state = dict(_state_for(key))
    state['available'] = available(key)
    state['size'] = total_size(key)
    return state


# --- Registry mutations ---

def add(model):
    """Add a user model. `model` must carry key/label/kind/engine/source."""
    for field in ('key', 'label', 'kind', 'engine', 'source'):
        if field not in model:
            raise ValueError(f'missing field: {field}')
    key = model['key']
    if not key or not all(c.isalnum() or c in '-_' for c in key) \
            or len(key) > 40:
        raise ValueError('invalid key')
    with _reg_lock:
        doc = load_registry()
        if any(m['key'] == key for m in doc['models']):
            raise ValueError(f'model {key} already exists')
        entry = {
            'key': key, 'label': model['label'], 'kind': model['kind'],
            'engine': model['engine'],
            'scale': model.get('scale'),
            'source': copy.deepcopy(model['source']),
            'size_bytes': int(model.get('size_bytes') or 0),
            'options': model.get('options') or {},
            'builtin': False,
        }
        doc['models'].append(entry)
        _write_registry(doc)
    _state_for(key)
    return copy.deepcopy(entry)


def remove(key):
    """Drop a model from the registry and delete its cached weights."""
    entry = get(key)
    if entry is None:
        raise KeyError(key)
    with _reg_lock:
        if _state_for(key)['status'] == 'downloading':
            raise RuntimeError(f'{key} is downloading')
        doc = load_registry()
        doc['models'] = [m for m in doc['models'] if m['key'] != key]
        _write_registry(doc)
    remove_weights_only(key)
    with _lock:
        _state.pop(key, None)


def _record_install(key, revision, etags):
    """Persist resolved revision/etag after a successful install."""
    with _reg_lock:
        doc = load_registry()
        for m in doc['models']:
            if m['key'] == key:
                m.setdefault('installed', {})
                m['installed'] = {
                    'revision': revision,
                    'etag': etags,
                    'installed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                }
                break
        _write_registry(doc)


def installed_revision(key):
    m = get(key) or {}
    return (m.get('installed') or {}).get('revision')


# --- Downloads ---

def start_download(key):
    """Kick off the model download in a background thread (idempotent)."""
    if not valid(key):
        raise KeyError(key)
    with _lock:
        state = _state_for(key)
        if state['status'] == 'downloading' or available(key):
            return
        state.update(status='downloading', progress=0, error=None)
    threading.Thread(target=_download, args=(key,), daemon=True).start()


def _progress_cb(key, total):
    def cb(done_bytes):
        with _lock:
            _state_for(key)['progress'] = min(99, int(done_bytes / total * 100)) if total else 0
    return cb


def _download(key):
    entry = get(key)
    if entry is None:
        return
    try:
        src = entry['source']
        if src.get('type') == 'hf' and not src.get('file'):
            revision, etags = _download_snapshot(key, entry, _progress_cb(key, total_size(key)))
        elif src.get('type') == 'url':
            revision, etags = _download_zip(key, entry, _progress_cb(key, total_size(key)))
        else:
            revision, etags = _download_files(key, entry)
        _record_install(key, revision, etags)
        with _lock:
            _state_for(key).update(status='done', progress=100)
        print(f'[models] {key} installed (rev {revision})', flush=True)
    except Exception as e:
        with _lock:
            _state_for(key).update(status='error', error=str(e)[:200])
        print(f'[models] {key} download failed: {e}', flush=True)


def _http_ctx():
    ctx = None
    try:
        import certifi
        import ssl
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    return ctx


def _fetch(url, timeout=30):
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'NextGenUp'})
    return urllib.request.urlopen(req, timeout=timeout, context=_http_ctx())


def _download_to(url, dest, on_chunk=None):
    """Stream url to dest(.part), returning the response ETag."""
    part = dest + '.part'
    with _fetch(url) as resp:
        etag = resp.headers.get('ETag')
        with open(part, 'wb') as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                if on_chunk:
                    on_chunk(len(chunk))
    os.replace(part, dest)
    return etag


def _download_files(key, entry):
    """Legacy single-file downloads (+ tar extraction) into models/."""
    dest_dir = paths.data('models')
    os.makedirs(dest_dir, exist_ok=True)
    total = total_size(key)
    etags = {}
    done_before = 0
    for spec in entry['source']['files']:
        url, name, approx = spec['url'], spec['name'], spec['bytes']
        dest = os.path.join(dest_dir, name)
        if os.path.exists(dest):
            done_before += approx
            continue
        acc = {'n': 0}
        def on_chunk(n, base=done_before):
            acc['n'] += n
            with _lock:
                _state_for(key)['progress'] = min(
                    99, int((base + acc['n']) / total * 100)) if total else 0
        etags[name] = _download_to(url, dest, on_chunk)
        done_before += approx
    extract = entry['source'].get('extract')
    if extract:
        archive = os.path.join(dest_dir, entry['source']['files'][0]['name'])
        _extract_tar(archive, dest_dir, extract)
        os.remove(archive)
    return entry['source'].get('revision'), etags


def _extract_tar(archive, dest_dir, members):
    """Pull named members out of a .tar.gz into dest_dir under new names."""
    import tarfile
    with tarfile.open(archive, 'r:gz') as tf:
        for member, out_name in members.items():
            src = tf.extractfile(member)
            if src is None:
                raise OSError(f'{member} missing from archive')
            with open(os.path.join(dest_dir, out_name), 'wb') as f:
                f.write(src.read())


def _extract_zip(archive, dest_dir, members):
    import zipfile
    with zipfile.ZipFile(archive) as zf:
        for member, out_name in members.items():
            with zf.open(member) as src, open(os.path.join(dest_dir, out_name), 'wb') as f:
                shutil.copyfileobj(src, f)


def _download_zip(key, entry, progress):
    """Download a single zip (e.g. QAIRT release assets) and extract it."""
    dest_dir = paths.data('models')
    os.makedirs(dest_dir, exist_ok=True)
    src = entry['source']
    archive = os.path.join(dest_dir, src['file'])
    etag = _download_to(src['url'], archive, lambda n: progress(_size_so_far(archive)))
    _extract_zip(archive, dest_dir, src['extract'])
    os.remove(archive)
    # resolve the pinned HF revision for update tracking
    revision = _resolve_revision(src.get('repo')) or src.get('revision')
    return revision, {src['file']: etag}


def _download_snapshot(key, entry, progress):
    """Snapshot-style HF download into models/<key>/."""
    src = entry['source']
    root = _model_dir(key)
    os.makedirs(root, exist_ok=True)
    revision = _resolve_revision(src['repo']) or src.get('revision') or 'main'
    files = _snapshot_files(entry)
    etags = {}
    for rel in files:
        dest = os.path.join(root, rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        url = f"{HF}/{src['repo']}/resolve/{revision}/{rel}"
        etags[rel] = _download_to(
            url, dest,
            lambda n: progress(_size_so_far(root)))
    return revision, etags


def _size_so_far(path_or_dir):
    """Bytes downloaded so far — either the .part file or a dir walk."""
    if os.path.isdir(path_or_dir):
        total = 0
        for dirpath, _, filenames in os.walk(path_or_dir):
            for fn in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
        return total
    part = path_or_dir + '.part'
    p = part if os.path.exists(part) else path_or_dir
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def _resolve_revision(repo):
    """Current commit sha of an HF repo, or None if unreachable."""
    if not repo:
        return None
    try:
        import urllib.request
        req = urllib.request.Request(
            f'{HF_API}/{repo}', headers={'User-Agent': 'NextGenUp'})
        with urllib.request.urlopen(req, timeout=15, context=_http_ctx()) as resp:
            import json as _json
            return _json.load(resp).get('sha')
    except Exception:
        return None


def needs_update(key):
    """True when a 'main'-pinned model's upstream revision moved."""
    entry = get(key)
    if entry is None:
        return False
    src = entry.get('source', {})
    if src.get('revision') != 'main' or not src.get('repo'):
        return False  # pinned revisions never auto-update
    installed = entry.get('installed') or {}
    if not installed.get('revision'):
        return not available(key)
    current = _resolve_revision(src['repo'])
    return bool(current and current != installed['revision'])


def update(key):
    """Re-download a model whose upstream moved. Returns True if started."""
    if not valid(key):
        raise KeyError(key)
    entry = get(key)
    src = entry.get('source', {})
    if src.get('revision') != 'main':
        return False  # pinned sha revisions never auto-update
    # drop cached weights so start_download sees them as gone
    if available(key):
        remove_weights_only(key)
    start_download(key)
    return True


def remove_weights_only(key):
    """Delete cached weights without touching the registry entry."""
    entry_dir = _model_dir(key)
    if os.path.isdir(entry_dir):
        shutil.rmtree(entry_dir, ignore_errors=True)
    entry = get(key) or {'source': {}}
    for name in _flat_names(entry):
        p = paths.find_model(name)
        if p and os.path.isfile(p) and paths.data('models') in os.path.abspath(p):
            try:
                os.remove(p)
            except OSError:
                pass
    with _lock:
        _state_for(key).update(status='idle', progress=0, error=None)
