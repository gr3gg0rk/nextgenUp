"""Engine environment management for GPU model tier.

Owns the uv-managed CPython 3.12 venv at paths.data('engine', 'venv'),
the uv bootstrap binary at paths.data('engine', 'bin'), and per-spec
install state in paths.data('engine', 'state.json').

Public API (contract v1, sections 2-3):
    status() -> dict
    gpu_info() -> dict
    ensure_installed(engine, progress_cb=None) -> dict
    update(engine, progress_cb=None) -> dict
    python_exe() -> str
"""

import hashlib
import json
import os
import shutil
import subprocess
import threading
import urllib.request
import zipfile

import paths

ENGINE_ROOT = paths.data(os.path.join('engine'))
BIN_DIR = paths.data(os.path.join('engine', 'bin'))
VENV_DIR = paths.data(os.path.join('engine', 'venv'))
STATE_PATH = paths.data(os.path.join('engine', 'state.json'))

# Pinned standalone uv release (win-amd64). Bump deliberately.
UV_VERSION = '0.8.22'
UV_URL = ('https://github.com/astral-sh/uv/releases/download/'
          f'{UV_VERSION}/uv-x86_64-pc-windows-msvc.zip')
UV_SHA256 = None  # verified at bootstrap when known; fetch .sha256 sidecar otherwise
UV_EXE = os.path.join(BIN_DIR, 'uv.exe')
UVX_EXE = os.path.join(BIN_DIR, 'uvx.exe')

# CPython: the official python.org build — SAC trusts its binaries (uv's
# standalone CPython has low-reputation DLLs that Smart App Control blocks,
# WinError 4551). Shipped via the embeddable zip (see _ensure_py_home); uv
# still manages the venv on top of it under engine/pythons.
PY_VERSION_FULL = '3.12.10'
# Embeddable distribution: pure zip extraction. The python.org *installer*
# bundle is not deployable headlessly on hosts with Smart App Control
# enforcing — its bootstrap engine has no cloud verdict and is blocked
# (exit 66/67, ~empty log), which surfaces as 'python install failed'.
PY_EMBED_URL = ('https://www.python.org/ftp/python/'
                f'{PY_VERSION_FULL}/python-{PY_VERSION_FULL}-embed-amd64.zip')
PY_EMBED_SHA256 = ('4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c'
                   '8099a63025a3c3')
PY_PTH_NAME = 'python' + ''.join(PY_VERSION_FULL.split('.')[:2]) + '._pth'
PY_HOME = os.path.join(ENGINE_ROOT, 'pythons', 'cpy312')
PY_HOME_EXE = os.path.join(PY_HOME, 'python.exe')

# torch index: sm_120 needs cu128 wheels; pin local versions so uv cannot
# resolve the CPU builds from PyPI (unsafe-best-match + explicit +cu128).
TORCH_PINS = ['torch==2.7.1+cu128', 'torchvision==0.22.1+cu128']
TORCH_INDEX = 'https://download.pytorch.org/whl/cu128'

# Shared torch stack (sm_120 needs torch>=2.7 cu128 wheels) + runtime libs.
_TORCH_STACK = TORCH_PINS + [
    'numpy', 'pillow',
]
_TRANSFORMERS_STACK = [
    # <5: diffusers 0.34 imports FLAX_WEIGHTS_NAME, removed in transformers 5
    'transformers>=4.45,<5',
    'safetensors',
    'pillow',
    'numpy',
    'huggingface-hub',
]
_ORT_STACK = [
    # 1.22.x = last CUDA 12.x line (1.31+ needs CUDA 13 + its own cuDNN).
    'onnxruntime-gpu==1.22.0',
    'numpy',
    'pillow',
]

# Per-engine dependency specs. installed-on-first-use; "Update engine"
# runs uv pip install -U for the spec's deps.
ENGINE_SPECS = {
    'torch-swin2sr': {
        'label': 'Swin2SR (torch, CUDA)',
        'venv': 'venv',
        'deps': _TORCH_STACK + _TRANSFORMERS_STACK,
        'torch_index': True,
    },
    'onnx-gpu': {
        'label': 'ONNX Runtime GPU (CUDA EP)',
        'venv': 'venv',
        'deps': _ORT_STACK,
        'torch_index': False,
    },
    'onnx': {
        'label': 'ONNX Runtime (CPU, shared env)',
        'venv': 'venv',
        'deps': ['onnxruntime', 'numpy', 'pillow'],
        'torch_index': False,
    },
    # --- Finalized per primary-source scout reports (Main). ---
    'seedvr2': {
        'label': 'SeedVR2-3B video restoration (torch, CUDA)',
        'venv': 'venv',
        'deps': _TORCH_STACK + [
            'safetensors', 'numpy', 'tqdm', 'psutil', 'einops',
            'transformers>=4.46,<5',
            'omegaconf>=2.3.0', 'diffusers>=0.33.1', 'peft>=0.17.0',
            'rotary_embedding_torch>=0.5.3', 'opencv-python', 'gguf',
            'matplotlib',
        ],
        'torch_index': True,
        # inference code itself: cloned (not pip) by _ensure_repo
        'git': {
            'url': 'https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git',
            'ref': '4490bd1f482e026674543386bb2a4d176da245b9',
            'dirname': 'ComfyUI-SeedVR2_VideoUpscaler',
            'to': 'src',
        },
    },
    'ltx-upscaler': {
        'label': 'LTX video spatial upscaler (torch, CUDA)',
        'venv': 'venv',
        'deps': _TORCH_STACK + [
            'diffusers==0.34.0', 'accelerate', 'imageio', 'imageio-ffmpeg',
            'av', 'huggingface_hub', 'transformers>=4.46,<5',
        ],
        'torch_index': True,
    },
}

_LOCK = threading.Lock()


# ---------------------------------------------------------------- state ----

def _load_state():
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def _spec_hash(engine):
    spec = ENGINE_SPECS[engine]
    blob = json.dumps({'deps': spec['deps'],
                       'torch_index': spec.get('torch_index', False),
                       'python': PY_VERSION_FULL}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _installed(engine):
    """True when venv exists and state.json records this exact spec hash."""
    state = _load_state()
    entry = state.get(engine)
    return bool(entry) and entry.get('spec_hash') == _spec_hash(engine)


# ------------------------------------------------------------------ uv -----

def _download(url, dest, progress_cb=None):
    req = urllib.request.Request(url, headers={'User-Agent': 'nextgenup-engine-env'})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, 'wb') as f:
        total = int(resp.headers.get('Content-Length') or 0)
        got = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress_cb and total:
                progress_cb(int(got * 100 / total), f'downloading {os.path.basename(dest)}')


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _ensure_uv(progress_cb=None):
    if os.path.exists(UV_EXE):
        return UV_EXE
    os.makedirs(BIN_DIR, exist_ok=True)
    zpath = os.path.join(BIN_DIR, f'uv-{UV_VERSION}.zip')
    _download(UV_URL, zpath, progress_cb)
    # Verify against the official .sha256 sidecar for the pinned version.
    shapath = zpath + '.sha256'
    try:
        _download(UV_URL + '.sha256', shapath)
        with open(shapath) as f:
            expected = f.read().split()[0].strip().lower()
        actual = _sha256_file(zpath)
        if actual != expected:
            os.remove(zpath)
            raise RuntimeError(f'uv zip sha256 mismatch: {actual} != {expected}')
    finally:
        with zipfile.ZipFile(zpath) as z:
            for member in ('uv.exe', 'uvx.exe'):
                with z.open(member) as src:
                    with open(os.path.join(BIN_DIR, member), 'wb') as out:
                        shutil.copyfileobj(src, out)
        for attempt in range(10):
            try:
                os.remove(zpath)
                break
            except PermissionError:
                import time
                time.sleep(0.2)
        else:
            zpath2 = zpath + '.del'
            try:
                os.replace(zpath, zpath2)
            except OSError:
                pass
    return UV_EXE


def _run(cmd, progress_cb=None, message='running', **kw):
    """Run a subprocess, streaming stderr lines as heartbeat progress."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', errors='replace', **kw)
    import threading as _t
    def pump():
        for line in proc.stderr:
            line = line.strip()
            if line and progress_cb:
                progress_cb(None, line[:160])
    t = _t.Thread(target=pump, daemon=True)
    t.start()
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f'command failed ({rc}): {" ".join(cmd)}')
    return proc


# -------------------------------------------------------------- public -----

def python_exe():
    """Absolute path to the engine venv python (creates nothing)."""
    return os.path.join(VENV_DIR, 'Scripts', 'python.exe')


def _ensure_py_home(progress_cb=None):
    """CPython 3.12 embeddable distribution under engine/pythons.

    The embeddable zip is pure file extraction — nothing executes during
    deploy, so Smart App Control cannot block it the way it blocks the
    python.org installer bundle's bootstrap engine."""
    if os.path.exists(PY_HOME_EXE):
        return PY_HOME_EXE
    os.makedirs(BIN_DIR, exist_ok=True)
    zpath = os.path.join(BIN_DIR, f'python-{PY_VERSION_FULL}-embed.zip')
    _download(PY_EMBED_URL, zpath, progress_cb)
    if _sha256_file(zpath) != PY_EMBED_SHA256:
        os.remove(zpath)
        raise RuntimeError('python embed zip sha256 mismatch')
    if progress_cb:
        progress_cb(10, f'unpacking CPython {PY_VERSION_FULL}')
    os.makedirs(PY_HOME, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(PY_HOME)
    try:
        os.remove(zpath)
    except OSError:
        pass
    if not os.path.exists(PY_HOME_EXE):
        raise RuntimeError('python install failed')
    # The embeddable package ships python312._pth with `import site`
    # commented out; venv/pip need site enabled plus a real site-packages.
    pth = os.path.join(PY_HOME, PY_PTH_NAME)
    with open(pth, 'r', encoding='utf-8') as f:
        content = f.read()
    content = content.replace('#import site', 'import site')
    if 'Lib/site-packages' not in content:
        content = content.rstrip('\n') + '\nLib/site-packages\n'
    with open(pth, 'w', encoding='utf-8') as f:
        f.write(content)
    return PY_HOME_EXE


def _bootstrap(progress_cb=None):
    """uv binary + trusted CPython 3.12 + venv. Idempotent."""
    _ensure_uv(progress_cb)
    if not os.path.exists(python_exe()):
        base = _ensure_py_home(progress_cb)
        if progress_cb:
            progress_cb(20, 'creating venv')
        env = dict(os.environ)
        _run([UV_EXE, 'venv', '--python', base, VENV_DIR],
             progress_cb, env=env)
    return python_exe()


def _pip(args, progress_cb=None, upgrade=False):
    base = [UV_EXE, 'pip', 'install']
    if upgrade:
        base.append('-U')
    env = dict(os.environ)
    env['VIRTUAL_ENV'] = VENV_DIR
    return _run(base + args, progress_cb, env=env)


def ensure_installed(engine, progress_cb=None):
    """Install the engine spec into the shared venv if not current."""
    if engine not in ENGINE_SPECS:
        raise ValueError(f'unknown engine: {engine}')
    with _LOCK:
        if _installed(engine):
            return {'installed': True, 'cached': True}
        report = _install_locked(engine, progress_cb, upgrade=False)
        return report


def update(engine, progress_cb=None):
    """Force re-resolve (+ -U) of the engine spec."""
    if engine not in ENGINE_SPECS:
        raise ValueError(f'unknown engine: {engine}')
    with _LOCK:
        return _install_locked(engine, progress_cb, upgrade=True)


def _git_exe():
    import shutil
    found = shutil.which('git')
    if found:
        return found
    for cand in (r'C:\Program Files\Git\cmd\git.exe',
                 r'C:\Program Files\Git\bin\git.exe',
                 '/usr/bin/git', '/opt/homebrew/bin/git'):
        if os.path.isfile(cand):
            return cand
    raise RuntimeError('git not found — required to fetch engine code')


def _ensure_repo(engine, spec, progress_cb=None):
    """Clone pinned non-pip inference code (e.g. the SeedVR2 wrapper)."""
    git_spec = spec.get('git')
    if not git_spec:
        return
    dest = os.path.join(ENGINE_ROOT, git_spec.get('to', 'src'),
                        git_spec['dirname'])
    if os.path.exists(os.path.join(dest, '.git')):
        return  # already cloned at the pinned ref
    if progress_cb:
        progress_cb(70, f'cloning {git_spec["dirname"]}')
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    git = _git_exe()
    _run([git, 'clone', git_spec['url'], dest], progress_cb)
    _run([git, '-C', dest, 'checkout', '--detach', git_spec['ref']],
         progress_cb)


def _install_locked(engine, progress_cb, upgrade):
    spec = ENGINE_SPECS[engine]
    if progress_cb:
        progress_cb(0, f'preparing engine env ({spec["label"]})')
    _bootstrap(progress_cb)

    args = list(spec['deps'])
    if spec.get('torch_index'):
        # cu128 wheels live on the pytorch index; PyPI is the extra for
        # transitive deps. unsafe-best-match + explicit +cu128 local pins
        # prevent uv from resolving the PyPI CPU builds.
        args = (['--index-strategy', 'unsafe-best-match',
                 '--index-url', TORCH_INDEX,
                 '--extra-index-url', 'https://pypi.org/simple'] + args)
    if progress_cb:
        progress_cb(30, f'installing {engine} deps')
    _pip(args, progress_cb, upgrade=upgrade)
    _ensure_repo(engine, spec, progress_cb)

    versions = _collect_versions(spec)
    state = _load_state()
    state[engine] = {
        'spec_hash': _spec_hash(engine),
        'installed_at': __import__('time').time(),
        'versions': versions,
    }
    _save_state(state)
    if progress_cb:
        progress_cb(100, 'engine ready')
    return {'installed': True, 'cached': False, 'versions': versions}


def _collect_versions(spec):
    """Best-effort version probe via the venv python."""
    mods = []
    for dep in spec['deps']:
        name = dep.split('>=')[0].split('<')[0].split('==')[0].strip()
        if name not in mods:
            mods.append(name)
    probe = ['import importlib.metadata as m, json',
             f'mods = {mods!r}',
             'out = {}',
             'for x in mods:',
             '    try: out[x] = m.version(x)',
             '    except Exception: pass',
             'import json; print(json.dumps(out))']
    try:
        r = subprocess.run([python_exe(), '-c', '\n'.join(probe)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        pass
    return {}


def _nvidia_smi_info():
    smi = shutil.which('nvidia-smi')
    if not smi:
        return None
    try:
        out = subprocess.run(
            [smi, '--query-gpu=name,memory.total,driver_version',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=10).stdout.strip()
        name, mem, drv = [x.strip() for x in out.splitlines()[0].split(',')]
        return {'name': name, 'vram_gb': round(float(mem) / 1024, 1),
                'driver': drv}
    except Exception:
        return None


def gpu_info():
    """GPU info. nvidia-smi before install; torch once the venv exists."""
    base = _nvidia_smi_info() or {'available': False}
    info = {'available': bool(base), **base}
    if os.path.exists(python_exe()):
        probe = '\n'.join([
            'import json',
            'try:',
            '    import torch',
            '    r = {"torch": torch.__version__, "cuda": torch.cuda.is_available()}',
            '    if torch.cuda.is_available():',
            '        p = torch.cuda.get_device_properties(0)',
            '        r.update(name=p.name, vram_gb=round(p.total_memory/2**30, 1),',
            '                 cc=f"{p.major}.{p.minor}")',
            'except Exception as e:',
            '    r = {"torch_error": str(e)}',
            'print(json.dumps(r))'])
        try:
            r = subprocess.run([python_exe(), '-c', probe],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                info['torch'] = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception as e:
            info['torch'] = {'torch_error': str(e)}
    return info


def status():
    state = _load_state()
    engines = {}
    for name, spec in ENGINE_SPECS.items():
        entry = state.get(name, {})
        engines[name] = {
            'label': spec['label'],
            'installed': _installed(name),
            'installed_at': entry.get('installed_at'),
            'versions': entry.get('versions', {}),
        }
    return {
        'engine_root': ENGINE_ROOT,
        'python': PY_VERSION_FULL,
        'venv_ready': os.path.exists(python_exe()),
        'uv': UV_VERSION if os.path.exists(UV_EXE) else None,
        'engines': engines,
        'gpu': gpu_info(),
    }
