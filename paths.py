"""Path resolution for both dev mode and the packaged desktop app.

Dev mode: everything lives in the repo directory as before.
Packaged (PyInstaller sidecar inside Tauri): read-only assets are unpacked
to sys._MEIPASS, writable data goes to NEXTGENUP_DATA (the Tauri app-data
dir), and ffmpeg/ffprobe sit next to the executable.
"""

import os
import sys

# Frozen sidecar: Tauri spawns us without a console, so sys.stdout/stderr
# wrap invalid handles and every print(flush=True) raises
# "[Errno 22] Invalid argument" — the download thread's except-clause then
# reports that as the task error, clobbering the 'done' status set a line
# earlier. Replace dead streams with devnull before anything logs.
for _stream_name in ('stdout', 'stderr'):
    _stream = getattr(sys, _stream_name, None)
    if _stream is None:
        setattr(sys, _stream_name, open(os.devnull, 'w', encoding='utf-8'))
        continue
    try:
        _stream.flush()
        os.fstat(_stream.fileno())
    except (OSError, ValueError, AttributeError):
        try:
            setattr(sys, _stream_name,
                    open(os.devnull, 'w', encoding='utf-8'))
        except OSError:
            pass

BUNDLE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('NEXTGENUP_DATA') or os.getcwd()

FROZEN = bool(getattr(sys, 'frozen', False))
EXE_DIR = os.path.dirname(sys.executable) if FROZEN else None


def resource(rel):
    """Read-only bundled asset (templates, static, small models)."""
    return os.path.join(BUNDLE_DIR, rel)


def data(rel):
    """Writable location (uploads, outputs, frames, tasks.db)."""
    return os.path.join(DATA_DIR, rel)


def find_model(name):
    """Look for an ONNX model: user's data dir first (big optional models
    like GFPGAN get dropped there), then the bundled static folder."""
    for p in (os.path.join(DATA_DIR, 'models', name),
              resource(os.path.join('static', 'models', name))):
        if os.path.exists(p):
            return p
    return None
