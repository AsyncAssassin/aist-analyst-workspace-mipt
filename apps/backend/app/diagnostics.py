"""Write bounded diagnostics without passing exception objects to logging."""
from collections import deque


def _symbol(value):
    if isinstance(value, str) and len(value) <= 96 and all(part.isidentifier() for part in value.split('.')):
        return value
    return '?'


def failure_message(event, stage, exception=None, *, operation=None, request_id=None,
                    status=None, code=None, worker_id=None, run_id=None, epoch=None):
    # Context comes from registered routes, contract errors and persisted or generated internal IDs.
    fields = (
        ('operation', operation), ('request_id', request_id), ('status', status), ('code', code),
        ('worker_id', worker_id), ('run_id', run_id), ('epoch', epoch),
    )
    parts = [event, f'stage={stage}']
    parts.extend(f'{name}={value}' for name, value in fields if value is not None)
    if exception is not None:
        parts.append(f'exception={_symbol(type(exception).__name__)}')
        frames = deque(maxlen=12)
        trace = exception.__traceback__
        # Follow only the traceback itself, with a bound on traversal and retained frames.
        for _ in range(64):
            if trace is None:
                break
            frame = trace.tb_frame
            module = _symbol(frame.f_globals.get('__name__'))
            function = _symbol(frame.f_code.co_name)
            frames.append(f'{module}.{function}:{trace.tb_lineno}')
            trace = trace.tb_next
        if frames:
            parts.append('stack=' + '>'.join(frames))
        if trace is not None:
            parts.append('stack_truncated=true')
    return ' '.join(parts)


def write_failure(logger, message):
    # Emit after leaving the exception handler. A broken stream's default logging
    # error reporter can print the active exception through implicit chaining.
    if message is None:
        return
    try:
        logger.warning(message)
    except Exception:
        # A custom log handler can also raise directly.
        pass
