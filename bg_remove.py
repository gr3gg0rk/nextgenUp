"""Server-side background removal via BiRefNet-lite (ONNX Runtime, CPU).

The model (~224MB, MIT license) is too large to bundle in the installer,
so it is downloaded on first use into the writable data dir — the same
drop-in folder GFPGAN uses, so offline users can place it manually.
"""

import threading

import numpy as np
from PIL import Image, ImageFilter

import model_store

INPUT_SIZE = 1024
# ImageNet normalization (from the model's preprocessor_config.json)
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

_session = None
_session_lock = threading.Lock()
_infer_lock = threading.Lock()


def model_path():
    return model_store.model_path('bg')


def available():
    return model_store.available('bg')


def download_status():
    return model_store.status('bg')


def start_download():
    model_store.start_download('bg')


def _get_session():
    global _session
    import onnxruntime as ort
    with _session_lock:
        if _session is None:
            _session = ort.InferenceSession(
                model_path(), providers=['CPUExecutionProvider']
            )
        return _session


def _predict_mask(img):
    """Run BiRefNet on a PIL image, return an alpha mask at the image's size."""
    sess = _get_session()
    input_name = sess.get_inputs()[0].name

    small = img.resize((INPUT_SIZE, INPUT_SIZE), Image.BILINEAR)
    arr = np.asarray(small, dtype=np.float32) / 255.0
    arr = (arr - MEAN) / STD
    inp = arr.transpose(2, 0, 1)[np.newaxis]

    with _infer_lock:
        out = sess.run(None, {input_name: np.ascontiguousarray(inp)})[0]

    mask = np.squeeze(out).astype(np.float32)
    # The export emits logits; apply sigmoid unless already in [0, 1]
    if mask.min() < -0.01 or mask.max() > 1.01:
        mask = 1.0 / (1.0 + np.exp(-mask))
    mask8 = (np.clip(mask, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)

    alpha = Image.fromarray(mask8, mode='L').resize(img.size, Image.LANCZOS)
    # Light feather so edges (hair, fur) blend instead of looking cut out
    return alpha.filter(ImageFilter.GaussianBlur(radius=1))


def remove_background(in_path, out_path, background='transparent',
                      progress_cb=None):
    """Remove the background from an image. background: 'transparent' or a
    '#rrggbb' color. Saves PNG, returns (width, height)."""
    img = Image.open(in_path).convert('RGB')
    if progress_cb:
        progress_cb(30, 'Running AI segmentation...')

    alpha = _predict_mask(img)
    if progress_cb:
        progress_cb(85, 'Compositing...')

    cutout = img.convert('RGBA')
    cutout.putalpha(alpha)

    if background == 'transparent':
        result = cutout
    else:
        color = background.lstrip('#')
        rgb = tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
        result = Image.new('RGBA', img.size, rgb + (255,))
        result.alpha_composite(cutout)
        result = result.convert('RGB')

    result.save(out_path, 'PNG')
    return img.size
