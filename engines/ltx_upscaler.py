"""LTX video spatial upscaler (feed-forward latent upsample, 2x spatial).

Spec from primary-source research (Main):
- LTXLatentUpsamplePipeline("Lightricks/ltxv-spatial-upscaler-0.9.7"), bf16
  on cuda; pipe.vae.enable_tiling(); ~2.8GB resident + tiled decode
  ~= 5-6GB peak at 720p->1440p. OOM fallback: enable_model_cpu_offload().
- H,W must be %32==0: pad in, crop out.
- Frame count must be 8k+1 (pipeline silently truncates): windows of
  <=121 frames, each padded/trimmed to 8k+1. Spatial-only per-frame op,
  windows do not degrade quality.
- Scale fixed 2x; --scale 4 runs the pipeline twice.
- Pipeline drops audio: server muxes it back (not this runner's job).
- License: LTXV Open Weights (kept in the weights snapshot dir).

Progress: one NDJSON line per window (encode/upsample/decode are
monolithic inside the pipeline; windows are the only granularity).
"""

import os

_REPO_ID = 'Lightricks/ltxv-spatial-upscaler-0.9.7'
_WINDOW = 121      # <=121 frames per pass (model ceiling <257)
_ALIGN = 32        # H,W multiple
_FRAME_ALIGN = 8   # frame count 8k+1


def _windows(n, size):
    """Split frame count into overlapping windows of `size`, each padded
    to 8k+1 at load time (frames are spatial-only; no temporal blending
    needed beyond exact continuation: windows are contiguous, last window
    repeats its final frame padding which we trim)."""
    steps = []
    start = 0
    while start < n:
        end = min(n, start + size)
        steps.append((start, end))
        if end == n:
            break
        start = end  # contiguous windows; frame ops are independent
    return steps


def _pad_to_8k1(count):
    r = (count - 1) % _FRAME_ALIGN
    return count if r == 0 else count + (_FRAME_ALIGN - r)


def run(weights_dir, input_path, output_path, scale=None, options=None,
        progress_cb=None):
    import torch
    import numpy as np
    from PIL import Image, ImageOps

    passes = 1
    if scale not in (None, 2, 4):
        raise ValueError(f'ltx-upscaler supports scale 2 or 4 (got {scale})')
    if scale == 4:
        passes = 2

    opts = options or {}
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    dtype = torch.bfloat16 if device == 'cuda' else torch.float32

    if progress_cb:
        progress_cb(2, 'loading LTX spatial upscaler')
    from diffusers import LTXLatentUpsamplePipeline
    src = weights_dir if weights_dir and os.path.isdir(weights_dir) \
        and os.path.exists(os.path.join(weights_dir, 'model_index.json')) \
        else _REPO_ID
    pipe = LTXLatentUpsamplePipeline.from_pretrained(src, torch_dtype=dtype)
    pipe = pipe.to(device)
    pipe.vae.enable_tiling()

    frames = _load_frames(input_path)
    if not frames:
        raise ValueError(f'no frames decoded from {input_path}')

    for p in range(passes):
        w, h = frames[0].size
        pw, ph = (-w) % _ALIGN, (-h) % _ALIGN
        wins = _windows(len(frames), _WINDOW)
        total = len(wins)
        out_frames = []
        for i, (a, b) in enumerate(wins):
            chunk = frames[a:b]
            need = _pad_to_8k1(len(chunk))
            while len(chunk) < need:
                chunk.append(chunk[-1])  # repeat-pad; trimmed after
            padded = [_pad_img(f, pw, ph, ImageOps) for f in chunk]
            if progress_cb:
                progress_cb(2 + (p + i / total) * 96 / passes,
                            f'ltx pass {p + 1}/{passes} window {i + 1}/{total} '
                            f'(frames {a}-{b})')
            result = pipe(video=padded, height=h + ph, width=w + pw,
                          output_type='pil')
            out = result.frames[0][:len(chunk)]
            out_frames.extend(out)

        # crop padding: exact 2x of the original frame size
        fw, fh = w * 2, h * 2
        frames = [f.crop((0, 0, fw, fh)) for f in out_frames]
        if device == 'cuda':
            torch.cuda.empty_cache()

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or '.',
                exist_ok=True)
    if output_path.lower().endswith(('.mp4', '.mov', '.webm', '.mkv')):
        _write_video(frames, output_path, float(opts.get('fps', 30)), np)
    else:
        _write_gif(frames, output_path,
                   int(opts.get('frame_duration_ms', 100)))
    return {'output': output_path,
            'meta': {'engine': 'ltx-upscaler',
                     'scale': 2 * passes,
                     'frames': len(frames),
                     'passes': passes}}


def _pad_img(img, pw, ph, ImageOps):
    if not pw and not ph:
        return img
    return ImageOps.expand(img, border=(0, 0, pw, ph))


def _load_frames(path):
    from PIL import Image
    if path.lower().endswith(('.mp4', '.mov', '.avi', '.mkv', '.webm')):
        import imageio.v3 as iio
        return [Image.fromarray(f) for f in iio.imread(path, plugin='pyav')]
    im = Image.open(path)
    frames = []
    try:
        while True:
            frames.append(im.convert('RGB').copy())
            im.seek(im.tell() + 1)
    except EOFError:
        pass
    return frames


def _write_video(frames, out_path, fps, np):
    import imageio.v2 as iio
    w = iio.get_writer(out_path, fps=fps, codec='libx264',
                       quality=8, macro_block_size=None)
    for f in frames:
        w.append_data(np.asarray(f.convert('RGB')))
    w.close()


def _write_gif(frames, out_path, duration_ms):
    frames[0].save(out_path, save_all=True, append_images=frames[1:],
                   loop=0, duration=duration_ms)
