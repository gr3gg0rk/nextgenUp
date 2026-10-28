"""Engine implementations, dispatched by engine_runner.py.

Each entry: callable(weights_dir, input_path, output_path, scale, options,
progress_cb) -> {'output': path, 'meta': {...}}.
"""

from engines.torch_swin2sr import run as torch_swin2sr
from engines.onnx_gpu import run as onnx_gpu
from engines.seedvr2 import run as seedvr2
from engines.ltx_upscaler import run as ltx_upscaler

ENGINES = {
    'torch-swin2sr': torch_swin2sr,
    'onnx-gpu': onnx_gpu,
    'onnx': onnx_gpu,
    'seedvr2': seedvr2,
    'ltx-upscaler': ltx_upscaler,
}
