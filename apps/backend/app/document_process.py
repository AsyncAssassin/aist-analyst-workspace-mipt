"""Bounded system tools for parsing and rendering documents."""

import os
import signal
import subprocess


def document_command(command, *, seconds, data=None, home='/tmp', python_path=None):
    # Only document-processing settings cross this subprocess boundary.
    env = {'PATH': '/usr/bin:/bin', 'HOME': home, 'XDG_CACHE_HOME': home + '/cache', 'LANG': 'C.UTF-8'}
    if python_path is not None:
        env['PYTHONPATH'] = str(python_path)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
    process = subprocess.Popen(command, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                               start_new_session=True)
    try:
        stdout, _ = process.communicate(data, timeout=seconds)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        raise
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.stdout.close()
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command)
    return stdout
