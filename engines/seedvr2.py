"""SeedVR2-3B video restoration engine.

Per finalized spec (Main): replicate inference_cli.py's single-GPU path
from the ComfyUI-SeedVR2_VideoUpscaler repo, cloned to
engine/src/ComfyUI-SeedVR2_VideoUpscaler @ 4490bd1 (Apache-2.0) by the
model installer. Weights: HF numz/SeedVR2_comfyUI @ 09ced71
(seedvr2_ema_3b_fp8_e4m3fn.safetensors + ema_vae_fp16 + pos/neg_emb),
fetched/SHA-verified by the repo's own downloader.

Until the runtime clone + weights exist this raises NotImplementedEngine
(runner maps to {"error": ...} exit 3); installs still work via
ENGINE_SPECS['seedvr2'].

12GB defaults (registry options): batch_size 5, dit/vae offload to cpu,
vae tiling, sdpa attention, LAB color correction. OOM escalation:
blocks_to_swap 8 -> 32; last resort Q8_0.gguf.
"""

import logging
import os
import sys

_REPO_DIRNAME = 'ComfyUI-SeedVR2_VideoUpscaler'
_DEFAULTS = {
    'batch_size': 5,
    'dit_offload_device': 'cpu',
    'vae_offload_device': 'cpu',
    'vae_encode_tiled': True,
    'vae_decode_tiled': True,
    'attention_mode': 'sdpa',
    'color_correction': 'lab',
    'blocks_to_swap': 8,
}


class NotImplementedEngine(NotImplementedError):
    pass


def _find_repo():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cand = os.path.join(here, 'engine', 'src', _REPO_DIRNAME)
    return cand if os.path.isdir(os.path.join(cand, 'src')) else None


def _find_model_file(weights_dir, opts):
    import glob
    name = opts.get('model') or 'seedvr2_ema_3b_fp8_e4m3fn.safetensors'
    hits = glob.glob(os.path.join(weights_dir, '**', name), recursive=True)
    return hits[0] if hits else None


def run(weights_dir, input_path, output_path, scale=None, options=None,
        progress_cb=None):
    repo = _find_repo()
    if not repo:
        raise NotImplementedEngine(
            'SeedVR2 runtime not cloned yet: expected '
            f'engine/src/{_REPO_DIRNAME} (installer wave). Deps are in '
            'ENGINE_SPECS; weights land in the model cache dir.')
    model_file = _find_model_file(weights_dir, options or {})
    if not model_file:
        raise NotImplementedEngine(
            f'SeedVR2 weights not found under {weights_dir} '
            '(download via the model manager first).')

    # The repo logs to stdout via its logger - keep NDJSON clean by
    # routing everything to stderr before importing it.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        force=True)
    for noisy in ('urllib3', 'PIL', 'matplotlib'):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    sys.path.insert(0, repo)

    opts = dict(_DEFAULTS)
    opts.update(options or {})

    from src.core.generation_phases import run_inference  # noqa: E402
    from src.utils.downloads import download_weights  # noqa: E402

    def cb(phase, done, total):
        if progress_cb and total:
            progress_cb(done * 95 / total, f'{phase} {done}/{total}')

    download_weights(model_dir=weights_dir, progress_cb=cb)
    result = run_inference(
        video_path=input_path,
        output_path=output_path,
        model_file=model_file,
        batch_size=opts['batch_size'],
        dit_offload_device=opts['dit_offload_device'],
        vae_offload_device=opts['vae_offload_device'],
        vae_encode_tiled=opts['vae_encode_tiled'],
        vae_decode_tiled=opts['vae_decode_tiled'],
        attention_mode=opts['attention_mode'],
        color_correction=opts['color_correction'],
        blocks_to_swap=opts['blocks_to_swap'],
        res_w=opts.get('res_w'),
        res_h=opts.get('res_h'),
        max_resolution=opts.get('max_resolution'),
        preserve_vram=True,
        progress_callback=cb,
    )

    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    out = result if isinstance(result, str) and result else output_path
    return {'output': out,
            'meta': {'engine': 'seedvr2',
                     'model': os.path.basename(model_file),
                     'batch_size': opts['batch_size'],
                     'blocks_to_swap': opts['blocks_to_swap']}}
