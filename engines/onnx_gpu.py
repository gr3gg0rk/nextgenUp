"""ONNX super-resolution on the CUDA Execution Provider.

Same tiling pattern as ai_engine.upscale_array (TILE 128, PAD 12); model
file resolved from --weights-dir. Falls back to CPU EP when CUDA is
unavailable so the 'onnx' alias still works in this runner.
"""

import glob
import os

import numpy as np

TILE = 128
PAD = 12


def _find_model(weights_dir):
    hits = sorted(glob.glob(os.path.join(weights_dir, '*.onnx')))
    if not hits:
        raise FileNotFoundError(f'no .onnx in {weights_dir}')
    return hits[0]


def _ensure_cuda_dlls():
    """ORT-GPU needs cuDNN 9 / cuBLAS 12 DLLs; torch's cu128 wheel ships
    them in torch/lib. Prepend that dir before session creation — the
    CUDA EP silently falls back to CPU otherwise."""
    try:
        import torch
        lib = os.path.join(os.path.dirname(torch.__file__), 'lib')
        if os.path.isdir(lib):
            os.add_dll_directory(lib)
            os.environ['PATH'] = lib + os.pathsep + os.environ.get('PATH', '')
    except ImportError:
        pass


def run(weights_dir, input_path, output_path, scale=None, options=None,
        progress_cb=None):
    import onnxruntime as ort
    from PIL import Image

    _ensure_cuda_dlls()
    avail = ort.get_available_providers()
    providers = ['CUDAExecutionProvider', 'CPUExecutionProvider'] \
        if 'CUDAExecutionProvider' in avail else ['CPUExecutionProvider']
    so = ort.SessionOptions()
    sess = ort.InferenceSession(_find_model(weights_dir), so,
                                providers=providers)
    input_name = sess.get_inputs()[0].name
    out_scale = scale or 4

    img = Image.open(input_path).convert('RGB')
    arr = np.asarray(img, dtype=np.float32) / 255.0
    h, w = arr.shape[:2]
    out = np.empty((h * out_scale, w * out_scale, 3), dtype=np.float32)

    tiles_x = (w + TILE - 1) // TILE
    tiles_y = (h + TILE - 1) // TILE
    total = tiles_x * tiles_y
    done = 0

    for ty in range(tiles_y):
        for tx in range(tiles_x):
            sx, sy = tx * TILE, ty * TILE
            x0, y0 = max(0, sx - PAD), max(0, sy - PAD)
            x1, y1 = min(w, sx + TILE + PAD), min(h, sy + TILE + PAD)

            tile_arr = arr[y0:y1, x0:x1]
            inp = tile_arr.transpose(2, 0, 1)[np.newaxis]
            res = sess.run(None, {input_name: np.ascontiguousarray(inp)})[0]
            res = res[0].transpose(1, 2, 0)

            pl = (sx - x0) * out_scale
            pt = (sy - y0) * out_scale
            cw = (min(w, sx + TILE) - sx) * out_scale
            ch = (min(h, sy + TILE) - sy) * out_scale
            out[sy * out_scale:sy * out_scale + ch,
                sx * out_scale:sx * out_scale + cw] = res[pt:pt + ch, pl:pl + cw]

            done += 1
            if progress_cb:
                progress_cb(done * 90 / total, f'onnx tile {done}/{total}')

    result = np.clip(out, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path, 'PNG')
    return {'output': output_path,
            'meta': {'engine': 'onnx-gpu' if providers[0].startswith('CUDA')
                     else 'onnx-cpu', 'scale': out_scale, 'tiles': total,
                     'providers': sess.get_providers()}}
