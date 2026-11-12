"""Связь полностью полученного ответа с действующим транспортным учетом."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from jsonschema import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from .accounting import Accounting, OBSERVE_PAUSE, OBSERVE_SECONDS
from .billing_evidence import match_refusal, refusal_payload, unique_object
from .database import temporarily_unavailable
from .contracts import validate_definition
from .errors import ApiError
from .live_profile import input_limit, profile_for
from .runs import execute, json_value
from .service import row

RUN_ID = re.compile(r'run_[a-f0-9]{32}\Z')
DIRECTORY = os.O_DIRECTORY | os.O_NOFOLLOW
STREAMS = ('https_openai_responses', 'https_anthropic_messages')


def binding(run, call):
    data = call['data']
    return dict(run_id=run['id'], call_id=data['id'], owner_id=run['owner_id'],
                project_id=run['project_id'], epoch=call['epoch'], attempted_at=data['attempted_at'],
                profile_id=data['model_profile_version_id'], purpose=data['purpose'])


def active(connection, run, epoch):
    if run['state'] != 'running' or run['epoch'] != epoch or run['worker_id'] is None:
        return False
    return execute(connection, 'SELECT lease_until>clock_timestamp() FROM aist.runs WHERE id=:id',
                   id=run['id']).scalar_one()


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def sync_directory(path):
    descriptor = os.open(path, DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(root, audit, run, call, sent, raw, protocol, observed_at, request_id, *, status=200, endpoint=None):
    root = Path(root).absolute()
    audit = audit.absolute()
    try:
        directory = audit.relative_to(root)
    except ValueError:
        # Some in-process transport consumers use their own temporary audit directory.
        return
    identifier = call['id']
    suffix = '.sse' if status == 200 and protocol in STREAMS else '.json'
    names = (identifier + '-request.json', identifier + '-response' + suffix)
    for name in names:
        with (audit / name).open('rb') as evidence:
            os.fsync(evidence.fileno())
    for part in (audit, *audit.parents):
        sync_directory(part)
        if part == root:
            break

    index = root / '.received'
    index.mkdir(mode=0o700, exist_ok=True)
    sync_directory(index)
    destination = index / run['id']
    destination.mkdir(mode=0o700, exist_ok=True)
    sync_directory(destination)
    manifest = dict(version=1, status=status, audit=directory.as_posix(), protocol=protocol,
                    binding=binding(run, dict(data=call, epoch=run['epoch'])),
                    request_sha256=sha256(sent), response_sha256=sha256(raw),
                    observed_at=observed_at, request_id=request_id)
    if status != 200:
        manifest.update(version=2, endpoint=endpoint)
    with tempfile.NamedTemporaryFile(dir=destination, prefix='.tmp-', delete=False) as temporary:
        path = Path(temporary.name)
        try:
            temporary.write(json_value(manifest).encode())
            temporary.flush()
            os.fsync(temporary.fileno())
            os.link(path, destination / (identifier + '.json'))
        finally:
            path.unlink()
    sync_directory(destination)
    sync_directory(destination.parent)
    sync_directory(root)
    return manifest


def read_evidence(root, relative, limit):
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('invalid evidence path')
    descriptor = os.open(root, DIRECTORY)
    try:
        for part in relative.parts[:-1]:
            nested = os.open(part, DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = nested
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(fd, 'rb') as evidence:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                raise ValueError('invalid evidence size or type')
            value = evidence.read(limit + 1)
            if len(value) != info.st_size:
                raise ValueError('evidence changed during read')
            return value
    finally:
        os.close(descriptor)


def load_response(root, run, call):
    identifier = call['data']['id']
    profile = profile_for(run['input'])
    protocol = profile['transport']
    manifest = json.loads(read_evidence(root, Path('.received', run['id'], identifier + '.json'), 8192),
                          object_pairs_hook=unique_object)
    expected = binding(run, call)
    status, version = manifest['status'], manifest['version']
    valid_version = (type(version) is int and type(status) is int
                     and (version == 1 and status == 200 or version == 2 and 400 <= status <= 599))
    if (not valid_version or manifest['protocol'] != protocol or manifest['binding'] != expected
            or expected['profile_id'] != run['input']['model_profile_version_id']):
        raise ValueError('invalid response binding')
    validate_definition('Timestamp', manifest['observed_at'])
    if manifest['request_id'] is not None and not isinstance(manifest['request_id'], str):
        raise ValueError('invalid request identifier')
    audit = Path(manifest['audit'])
    if audit.is_absolute() or '..' in audit.parts or not audit.parts:
        raise ValueError('invalid audit path')
    request = read_evidence(root, audit / (identifier + '-request.json'), input_limit(run['input'], expected['purpose']))
    suffix = '.sse' if status == 200 and protocol in STREAMS else '.json'
    raw = read_evidence(root, audit / (identifier + '-response' + suffix), profile.get('response_bytes', 2000000))
    if (sha256(request) != manifest['request_sha256'] or sha256(raw) != manifest['response_sha256']
            or json.loads(request)['model'] != profile['model']):
        raise ValueError('invalid response bytes')
    if version == 2:
        payload = refusal_payload(raw)
        if match_refusal(run, status, payload, manifest['endpoint'], manifest['request_id']) is None:
            raise ValueError('no pinned refusal rule')
        return manifest, payload
    if protocol == 'https_openai_responses':
        from .openai_responses import response
        payload = response(raw)
    elif protocol == 'https_anthropic_messages':
        from .anthropic_messages import response
        payload = response(raw)
    else:
        payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get('model') != profile['model']:
        raise ValueError('unexpected response model')
    return manifest, payload


def recover_observed(runs, run_id, *, wait=True):
    """Inspect files without a transaction, then recheck the call under the ledger locks."""
    from .note_external import recover_web_requests

    result = dict(run_id=run_id, calls=[], web=[])
    try:
        with runs.engine.connect() as connection:
            run = row(connection, 'SELECT * FROM aist.runs WHERE id=:id', id=run_id)
            if run is None:
                raise ApiError('resource_not_found')
            calls = list(execute(connection, 'SELECT * FROM aist.model_calls WHERE run_id=:id ORDER BY id',
                                 id=run_id).mappings())
            owned = active(connection, run, run['epoch'])
    except SQLAlchemyError as error:
        if not temporarily_unavailable(error):
            raise
        return dict(result, status='temporarily_unavailable')
    root = Path(runs.settings.openclaw_root)
    ledger = Accounting(runs)
    for call in calls:
        status = call['data']['transport_state']
        if status == 'observed':
            outcome = 'already_accounted'
        elif owned and run['epoch'] == call['epoch']:
            outcome = 'active'
        elif status not in ('sent', 'unknown') or not call['data']['sent_at']:
            outcome = 'insufficient_evidence'
        else:
            try:
                manifest, payload = load_response(root, run, call)
            except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, RecursionError, ValidationError):
                outcome = 'insufficient_evidence'
            else:
                deadline = time.monotonic() + OBSERVE_SECONDS
                while True:
                    try:
                        outcome = ledger.observe(run_id, call['epoch'], call['id'], manifest['status'], payload,
                            observed_at=manifest['observed_at'], request_id=manifest['request_id'],
                            native=manifest['protocol'] == 'https_anthropic_messages',
                            protocol=manifest['protocol'], deadline=deadline, saved_response=manifest['binding'],
                            refusal=manifest if manifest['version'] == 2 else None)
                        break
                    except SQLAlchemyError as error:
                        if not temporarily_unavailable(error):
                            raise
                        outcome = 'temporarily_unavailable'
                        remaining = deadline - time.monotonic()
                        if not wait or remaining <= OBSERVE_PAUSE:
                            break
                        time.sleep(OBSERVE_PAUSE)
        result['calls'].append(dict(call_id=call['id'], status=outcome))
    try:
        result['web'] = recover_web_requests(runs, run_id)
    except SQLAlchemyError as error:
        if not temporarily_unavailable(error):
            raise
        result['status'] = 'temporarily_unavailable'
    return result


def recover_saved_runs(runs):
    """Startup after a worker which fenced its own Run before exiting."""
    root = Path(runs.settings.openclaw_root) / '.received'
    with runs.engine.connect() as connection:
        candidates = {item[0] for item in execute(connection, """
            SELECT DISTINCT m.run_id FROM aist.model_calls m JOIN aist.runs r ON r.id=m.run_id
            WHERE m.data->>'transport_state' IN ('sent','unknown') AND m.data->>'sent_at' IS NOT NULL
            AND (r.state<>'running' OR r.worker_id IS NULL OR r.lease_until<=clock_timestamp() OR r.epoch<>m.epoch)
        """)}
        web_runs = {item[0] for item in execute(connection, """
            SELECT DISTINCT w.run_id FROM aist.web_requests w JOIN aist.runs r ON r.id=w.run_id
            WHERE w.state='reserved' AND (r.state<>'running' OR r.worker_id IS NULL
                OR r.lease_until<=clock_timestamp() OR r.epoch<>w.epoch)
        """)}
    return [recover_observed(runs, run_id, wait=False) for run_id in sorted(web_runs | {
        candidate for candidate in candidates if RUN_ID.fullmatch(candidate) and (root / candidate).is_dir()
    })]
