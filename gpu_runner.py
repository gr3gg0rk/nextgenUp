"""gpu_runner: server-side wrapper around engine_runner.py (contract section 3).

- global mutex: at most one GPU job at a time (12GB VRAM guard)
- subprocess.Popen of the ENGINE venv python on engine_runner.py
- stdout line pump -> NDJSON progress -> caller callback
- cancel = kill process tree
"""

import atexit
import json
import os
import subprocess
import threading

import engine_env
import paths

_LOCK = threading.Lock()
_ACTIVE = None  # (proc, cancel_event) while a GPU job runs
_ATFORK = threading.Lock()


class GPUBusyError(RuntimeError):
    pass


class EngineError(RuntimeError):
    pass


def _kill_tree(proc):
    try:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                       capture_output=True, timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


@atexit.register
def _cleanup(_kill_tree=_kill_tree):
    with _LOCK:
        if _ACTIVE and _ACTIVE[0].poll() is None:
            _kill_tree(_ACTIVE[0])


def run(engine, weights_dir, input_path, output_path, options,
        progress_cb, cancel_event, timeout=None):
    """Run one GPU job. Blocks until done; returns the runner's meta dict
    plus 'output'. Raises GPUBusyError if another GPU job is active,
    EngineError on runner failure or cancel."""
    if not _LOCK.acquire(timeout=0):
        raise GPUBusyError('another GPU job is already running')
    proc = None
    try:
        exe = engine_env.python_exe()
        if not os.path.exists(exe):
            raise EngineError('engine venv not installed')
        # Frozen: the runner ships as bundle data (see nextgenup-server.spec);
        # dev: it sits next to this module.
        runner = paths.resource('engine_runner.py')
        if not os.path.exists(runner):
            runner = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'engine_runner.py')
        cmd = [exe, runner,
               engine,
               '--weights-dir', weights_dir,
               '--input', input_path,
               '--output', output_path,
               '--options', json.dumps(options or {})]
        if options and options.get('scale'):
            cmd += ['--scale', str(int(options['scale']))]

        creationflags = 0x00000200 if os.name == 'nt' else 0  # CREATE_NEW_PROCESS_GROUP
        # The wrapper logs emoji to stdout; without this the child's charmap
        # codec raises UnicodeEncodeError on Windows.
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env,
                                text=True, encoding='utf-8', errors='replace',
                                creationflags=creationflags)
        with _ATFORK:
            globals()['_ACTIVE'] = (proc, cancel_event)

        meta = {}
        final_output = None
        err_tail = []

        def pump_stderr():
            for line in proc.stderr:
                err_tail.append(line.rstrip())
                del err_tail[:-20]

        et = threading.Thread(target=pump_stderr, daemon=True)
        et.start()

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if cancel_event is not None and cancel_event.is_set():
                _kill_tree(proc)
                raise EngineError('cancelled')
            if 'error' in obj:
                raise EngineError(obj['error'])
            pct = obj.get('progress')
            if pct is not None and 0 <= pct < 100 and progress_cb:
                progress_cb(pct, obj.get('message', ''))
            elif pct == 100:
                final_output = obj.get('output')
                meta = obj.get('meta', {})

        rc = proc.wait()
        if cancel_event is not None and cancel_event.is_set():
            raise EngineError('cancelled')
        if rc != 0 or not final_output:
            raise EngineError(f'runner exited rc={rc}: '
                              + (err_tail[-1] if err_tail else 'no output line'))
        return {'output': final_output, **meta}
    finally:
        if proc is not None and proc.poll() is None:
            _kill_tree(proc)
        with _ATFORK:
            globals()['_ACTIVE'] = None
        _LOCK.release()
