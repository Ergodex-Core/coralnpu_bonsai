"""Bounded local stages; never invokes a shell or a cloud API."""
import os
from pathlib import Path
import re
import signal
import subprocess


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check_log(path, markers=()):
    text = Path(path).read_text(errors='replace')
    require(
        not re.search(r'^\s*(?:ERROR:|FATAL:|%Error|\[ERROR\])', text, re.M),
        f'Error in {path}'
    )
    for marker in markers:
        require(
            len(re.findall(r'^' + re.escape(marker) + r'[^\n]*$', text,
                           re.M)) == 1,
            f'Missing or duplicate success marker {marker!r} in {path}'
        )


def run(command, log, cwd, markers=(), timeout=3600, env=None):
    """Terminate the whole stage process group on timeout, failure or interruption."""
    require(timeout > 0, 'Timeout must be positive')
    print(f'Running {log.stem}; log: {log}', flush=True)
    with log.open('w') as stream:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )
        try:
            code = process.wait(timeout=timeout)
            require(code == 0, f'Command failed ({code}); see {log}')
        finally:
            # Also remove descendants left behind by a completed parent.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    check_log(log, markers)
