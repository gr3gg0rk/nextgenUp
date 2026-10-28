"""Swin2SR image super-resolution via transformers, fp16 on CUDA.

Scale is fixed at 4 (realworld-sr-x4 checkpoints); other scales are
rejected rather than silently wrong. Large images are tiled on
multiple-of-8 windows (Swin2SR needs 8-divisible inputs) to fit VRAM.
"""

import numpy as np

TILE = 512          # window tile at input resolution (multiple of 8)
OVERLAP = 32        # context overlap between tiles
FADE = 16           # px feather when tiles touch (at output resolution)


def run(weights_dir, input_path, output_path, scale=None, options=None,
        progress_cb=None):
    import torch
    from PIL import Image, ImageOps
    from transformers import (Swin2SRForImageSuperResolution,
                              Swin2SRImageProcessor)

    if scale is not None and scale != 4:
        raise ValueError(f'swin2sr supports scale 4 only (got {scale})')

    opts = options or {}
    tile = int(opts.get('tile', TILE))
    dtype = torch.float16 if torch.cuda.is_available() else torch.float32
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    out_scale = 4

    if progress_cb:
        progress_cb(5, 'loading Swin2SR model')
    processor = Swin2SRImageProcessor.from_pretrained(weights_dir)
    model = Swin2SRForImageSuperResolution.from_pretrained(
        weights_dir, torch_dtype=dtype).to(device).eval()

    img = Image.open(input_path).convert('RGB')
    w, h = img.size
    out = np.zeros((h * out_scale, w * out_scale, 3), dtype=np.float32)
    weight = np.zeros((h * out_scale, w * out_scale, 1), dtype=np.float32)

    tiles_x = (w + tile - 1) // tile
    tiles_y = (h + tile - 1) // tile
    total = tiles_x * tiles_y
    done = 0

    for ty in range(tiles_y):
        for tx in range(tiles_x):
            sx, sy = tx * tile, ty * tile
            x0, y0 = max(0, sx - OVERLAP), max(0, sy - OVERLAP)
            x1, y1 = min(w, sx + tile + OVERLAP), min(h, sy + tile + OVERLAP)
            crop = img.crop((x0, y0, x1, y1))
            # Swin windows need 8-divisible inputs: reflect-pad the remainder
            pw, ph = (-crop.width) % 8, (-crop.height) % 8
            if pw or ph:
                crop = ImageOps.expand(crop, border=(0, 0, pw, ph), fill=0)

            inputs = processor(crop, return_tensors='pt').to(device, dtype)
            with torch.no_grad():
                res = model(**inputs).reconstruction.squeeze(0)
            tile_out = (res.clamp(0, 1).mul(255).byte()
                        .permute(1, 2, 0).cpu().numpy())
            if pw or ph:
                tile_out = tile_out[:crop.height - ph, :crop.width - pw]

            # paste core region only (drop overlap margin)
            cl, ct = (sx - x0) * out_scale, (sy - y0) * out_scale
            ccw = (min(w, sx + tile) - sx) * out_scale
            cch = (min(h, sy + tile) - sy) * out_scale
            piece = tile_out[ct:ct + cch, cl:cl + ccw].astype(np.float32)

            # feather where an already-written neighbour borders this core
            have = weight[sy:sy + cch, sx:sx + ccw, 0] > 0
            if have.any():
                fx = np.ones(ccw, np.float32)
                fy = np.ones(cch, np.float32)
                ramp = np.linspace(0.05, 1.0, FADE, dtype=np.float32)
                if have[0].any():
                    fy = np.where(have[0], ramp[:cch].clip(0.05, 1), 1.0)
                if have[:, 0].any():
                    fx = np.where(have[:, 0], ramp[:ccw].clip(0.05, 1), 1.0)
                piece *= (fy[:, None] * fx[None, :])[..., None]
                out[sy:sy + cch, sx:sx + ccw] += piece
                weight[sy:sy + cch, sx:sx + ccw, 0] = np.maximum(
                    weight[sy:sy + cch, sx:sx + ccw, 0],
                    (fy[:, None] * fx[None, :]))
            else:
                out[sy:sy + cch, sx:sx + ccw] = piece
                weight[sy:sy + cch, sx:sx + ccw, 0] = 1.0

            done += 1
            if progress_cb:
                progress_cb(done * 90 / total, f'swin2sr tile {done}/{total}')

    del model, inputs, res
    if device == 'cuda':
        torch.cuda.empty_cache()

    result = np.clip(out, 0, 255).astype(np.uint8)
    Image.fromarray(result).save(output_path, 'PNG')
    return {'output': output_path,
            'meta': {'engine': 'torch-swin2sr', 'scale': out_scale,
                     'tiles': total}}
