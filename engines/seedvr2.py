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
_REPO_REF = '4490bd1f482e026674543386bb2a4d176da245b9'
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
            'SeedVR2 runtime not cloned: expected '
            f'engine/src/{_REPO_DIRNAME} @ {_REPO_REF}. Install the engine '
            'from the model manager first.')
    model_file = _find_model_file(weights_dir, options or {})
    if not model_file:
        raise NotImplementedEngine(
            f'SeedVR2 weights not found under {weights_dir} '
            '(download via the model manager first).')

    # The repo logs to stdout via its logger - keep NDJSON clean by
    # routing everything to stderr before importing it.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        force=True)
    for noisy in ('urllib3', 'PIL', 'matplotlib', 'diffusers'):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    if repo not in sys.path:
        sys.path.insert(0, repo)

    opts = dict(_DEFAULTS)
    opts.update(options or {})

    # inference_cli.py:77 setdefaults PYTORCH_CUDA_ALLOC_CONF at import; if
    # torch loads first, the allocator parses the default backend at load
    # time and the env flip re-parses it at first CUDA touch -> "Allocator
    # backend parsed at runtime != at load time" INTERNAL ASSERT, poisoning
    # the context (access violation in the bf16 probe). Match the wrapper's
    # env BEFORE torch/cv2/inference_cli load.
    os.environ.setdefault('PYTORCH_CUDA_ALLOC_CONF', 'backend:cudaMallocAsync')

    import argparse
    import cv2
    import torch
    import inference_cli as ic

    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError(f'cannot open video: {input_path}')
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 24.0
    # max_frames=0 would read nothing (range(0)); pass a huge bound
    frames_tensor = ic._read_frames_from_cap(cap, 10 ** 9)
    cap.release()
    if frames_tensor is None or frames_tensor.shape[0] == 0:
        raise RuntimeError('no frames decoded from input video')
    frames_tensor = frames_tensor.to(torch.float16)

    # resolution = target short side (aspect preserved by the pipeline);
    # max_resolution=0 disables the long-side cap
    short = min(frames_tensor.shape[1], frames_tensor.shape[2])
    target = int(round(short * (scale or 2.0)))

    args = argparse.Namespace(**{
        'dit_model': os.path.basename(model_file),
        'vae_model': ic.DEFAULT_VAE,
        'model_dir': weights_dir,
        'resolution': target,
        'max_resolution': 0,
        'batch_size': opts['batch_size'],
        'uniform_batch_size': False,
        'seed': opts.get('seed', 42),
        'skip_first_frames': 0,
        'load_cap': 0,
        'chunk_size': 0,
        'prepend_frames': 0,
        'temporal_overlap': 0,
        'color_correction': opts['color_correction'],
        'input_noise_scale': 0.0,
        'latent_noise_scale': 0.0,
        'dit_offload_device': opts['dit_offload_device'],
        'vae_offload_device': opts['vae_offload_device'],
        'tensor_offload_device': 'cpu',
        'blocks_to_swap': opts['blocks_to_swap'],
        'swap_io_components': False,
        'vae_encode_tiled': opts['vae_encode_tiled'],
        'vae_encode_tile_size': 1024,
        'vae_encode_tile_overlap': 128,
        'vae_decode_tiled': opts['vae_decode_tiled'],
        'vae_decode_tile_size': 1024,
        'vae_decode_tile_overlap': 128,
        'tile_debug': 'false',
        'attention_mode': opts['attention_mode'],
        'compile_dit': False, 'compile_vae': False,
        'compile_backend': 'inductor', 'compile_mode': 'default',
        'compile_fullgraph': False, 'compile_dynamic': False,
        'compile_dynamo_cache_size_limit': 64,
        'compile_dynamo_recompile_limit': 128,
        'cache_dit': False, 'cache_vae': False,
        'debug': False,
        'allow_vram_overflow': False,
        'video_backend': 'ffmpeg', 'use_10bit': False,
    })

    # _process_frames_core passes progress_callback=None to the four phase
    # functions; they are resolved as module globals at call time, so
    # wrapping them here injects live NDJSON progress without forking the
    # wrapper's code. Phase spans follow the measured VAE-bound profile.
    spans = (('encode_all_batches', 5, 35),
             ('upscale_all_batches', 35, 62),
             ('decode_all_batches', 62, 96),
             ('postprocess_all_batches', 96, 99))
    originals = {}

    def _patched(orig, lo, hi):
        def pcb(done, total, frames, phase):
            if progress_cb and total:
                progress_cb(lo + (hi - lo) * (done / float(total)),
                            f'{phase} {done}/{total}')
        def call(*a, **k):
            k['progress_callback'] = pcb
            return orig(*a, **k)
        return call

    for name, lo, hi in spans:
        originals[name] = getattr(ic, name)
        setattr(ic, name, _patched(originals[name], lo, hi))
    try:
        result = ic._process_frames_core(
            frames_tensor=frames_tensor, args=args, device_id='0',
            debug=ic.debug, runner_cache=None)
    finally:
        for name, orig in originals.items():
            setattr(ic, name, orig)

    try:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    ic.save_frames_to_video(result.to(torch.float32), output_path,
                            fps=fps, video_backend='ffmpeg')
    return {'output': output_path,
            'meta': {'engine': 'seedvr2',
                     'model': os.path.basename(model_file),
                     'resolution': target,
                     'batch_size': opts['batch_size'],
                     'blocks_to_swap': opts['blocks_to_swap']}}
