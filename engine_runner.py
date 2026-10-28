"""Engine-side runner: executed by the ENGINE venv python (not the server venv).

Contract section 3 protocol:
    python engine_runner.py <engine> --weights-dir P --input P --output P
        [--scale N] [--options JSON] [--models-root P] [--selftest]

stdout carries one JSON object per line only:
    {"progress": 42, "message": "..."}                 (0-100, heartbeat)
    {"progress": 100, "output": "<abs path>", "meta": {...}}   (final)
    {"error": "..."}                                   (failure, exit 1)
stderr is free-form logs.

--selftest exercises the protocol itself: emits progress lines and writes
a small output file. Used to unit-test the pump without model weights.
"""

import argparse
import json
import os
import sys
import time

_ENGINES = None


def _emit(obj):
    sys.stdout.write(json.dumps(obj) + '\n')
    sys.stdout.flush()


def progress_cb(pct, message=''):
    if pct is None:
        _emit({'progress': -1, 'heartbeat': True, 'message': str(message)})
    else:
        _emit({'progress': max(0, min(100, int(pct))),
               'message': str(message)})


def _load_engine(name):
    global _ENGINES
    if _ENGINES is None:
        from engines import ENGINES
        _ENGINES = ENGINES
    if name not in _ENGINES:
        raise KeyError(f'unknown engine: {name}')
    return _ENGINES[name]


def _selftest(args):
    for i in range(1, 6):
        progress_cb(i * 15, f'selftest step {i}/5')
        time.sleep(0.05)
    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or '.', exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        f.write('selftest')
    _emit({'progress': 100, 'output': os.path.abspath(args.output),
           'meta': {'engine': 'selftest'}})
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='engine_runner')
    ap.add_argument('engine')
    ap.add_argument('--input', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--weights-dir')
    ap.add_argument('--models-root')
    ap.add_argument('--scale', type=int)
    ap.add_argument('--options', default='{}')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest(args)

    try:
        options = json.loads(args.options)
    except ValueError as e:
        _emit({'error': f'bad --options JSON: {e}'})
        return 2

    weights_dir = args.weights_dir
    if not weights_dir and args.models_root:
        # resolve from registry copy: models root keyed by model directory
        cand = os.path.join(args.models_root, args.engine)
        weights_dir = cand if os.path.isdir(cand) else args.models_root
    if not weights_dir:
        _emit({'error': 'no --weights-dir or --models-root given'})
        return 2

    try:
        fn = _load_engine(args.engine)
        result = fn(weights_dir=weights_dir, input_path=args.input,
                    output_path=args.output, scale=args.scale,
                    options=options, progress_cb=progress_cb)
        out = result.get('output') if isinstance(result, dict) else args.output
        meta = result.get('meta', {}) if isinstance(result, dict) else {}
        _emit({'progress': 100, 'output': os.path.abspath(out), 'meta': meta})
        return 0
    except NotImplementedError as e:
        _emit({'error': f'engine not implemented yet: {e}'})
        return 3
    except Exception as e:  # noqa: BLE001 - protocol boundary
        import traceback
        traceback.print_exc(file=sys.stderr)
        _emit({'error': f'{type(e).__name__}: {e}'})
        return 1


if __name__ == '__main__':
    sys.exit(main())
