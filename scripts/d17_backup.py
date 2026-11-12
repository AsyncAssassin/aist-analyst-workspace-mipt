"""Cold backup and restore of the four D17 volumes, never over existing data."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

from d17_preflight import check


INVENTORY = '''import hashlib,json
from pathlib import Path
root=Path('/volume');result={}
for path in [root,*sorted(root.rglob('*'))]:
    stat=path.lstat()
    assert not path.is_symlink(), 'Unexpected symlink'
    item=dict(mode=stat.st_mode,uid=stat.st_uid,gid=stat.st_gid)
    if path.is_file():
        with path.open('rb') as stream:item['sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    result[str(path.relative_to(root))]=item
print(json.dumps(result,sort_keys=True))
'''


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True)


def sha256(path):
    with path.open('rb') as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        return digest.hexdigest()


def inventory(volume, image):
    return json.loads(docker('run', '--rm', '--network', 'none', '--user', '0',
                             '-v', volume + ':/volume:ro', image, 'python', '-c', INVENTORY))


def check_archive(archive):
    regular_files = set()
    with tarfile.open(archive) as stream:
        for member in stream:
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('Unsafe archive member')
            if member.isfile():
                regular_files.add(path)
            elif member.islnk():
                # Export publication uses hard links to an earlier regular file.
                if Path(member.linkname) not in regular_files:
                    raise ValueError('Unsafe archive link')
            elif not member.isdir():
                raise ValueError('Unsafe archive member')


def main():
    action, directory = sys.argv[1:]
    config = json.load(sys.stdin)
    check(config)
    stack = config['name']
    volumes = {key: item['name'] for key, item in config['volumes'].items()}
    if set(volumes) != {'pgdata', 'sources', 'exports', 'qdrant'}:
        raise ValueError('Expected exactly four data volumes')
    for volume in volumes.values():
        if docker('ps', '-q', '--filter', 'volume=' + volume).strip():
            raise ValueError('Stop all processes using ' + volume)
    image = config['services']['backend']['image']
    directory = Path(directory).resolve()
    if action == 'backup':
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        # A raw PostgreSQL backup is valid only after a clean shutdown.
        control = docker('run', '--rm', '--network', 'none',
                         '-v', volumes['pgdata'] + ':/volume:ro',
                         config['services']['db']['image'], 'pg_controldata', '/volume')
        if 'shut down' not in control:
            raise ValueError('PostgreSQL was not shut down cleanly')
        manifest = dict(format='aist-d17-cold-v1', source_stack=stack, volumes={}, images={})
        for service in ('db', 'qdrant', 'backend', 'web'):
            reference = config['services'][service]['image']
            manifest['images'][service] = dict(reference=reference, id=docker('image', 'inspect', reference, '--format', '{{.Id}}').strip())
        for key, volume in volumes.items():
            before = inventory(volume, image)
            docker('run', '--rm', '--network', 'none', '--user', '0',
                   '-v', volume + ':/volume:ro', '-v', str(directory) + ':/backup',
                   image, 'tar', '--numeric-owner', '-cpf', '/backup/' + key + '.tar', '-C', '/volume', '.')
            after = inventory(volume, image)
            if before != after:
                raise ValueError('Volume changed during backup: ' + key)
            archive = directory / (key + '.tar')
            archive.chmod(0o600)
            manifest['volumes'][key] = dict(sha256=sha256(archive), entries=before)
        (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(json.dumps(dict(backup=str(directory), source_stack=stack, consistent=True)))
    elif action == 'restore':
        manifest = json.loads((directory / 'manifest.json').read_text())
        if manifest['format'] != 'aist-d17-cold-v1' or manifest['source_stack'] == stack:
            raise ValueError('Restore requires another D17 stack')
        if set(manifest['volumes']) != set(volumes):
            raise ValueError('Incomplete backup')
        existing = set(docker('volume', 'ls', '--format', '{{.Name}}').splitlines())
        if existing.intersection(volumes.values()):
            raise ValueError('Destination volumes already exist; choose a new D17 stack')
        # Verify every archive and image before creating any destination volume.
        for service, saved in manifest['images'].items():
            current = config['services'][service]['image']
            if docker('image', 'inspect', current, '--format', '{{.Id}}').strip() != saved['id']:
                raise ValueError('Restore requires the backed-up image: ' + service)
        for key, saved in manifest['volumes'].items():
            archive = directory / (key + '.tar')
            if sha256(archive) != saved['sha256']:
                raise ValueError('Archive checksum mismatch: ' + key)
            check_archive(archive)
        restored = {}
        for key, volume in volumes.items():
            docker('volume', 'create', '--label', 'com.docker.compose.project=' + stack,
                   '--label', 'com.docker.compose.volume=' + key, volume)
            docker('run', '--rm', '--network', 'none', '--user', '0',
                   '-v', volume + ':/volume', '-v', str(directory) + ':/backup:ro',
                   image, 'tar', '--numeric-owner', '-xpf', '/backup/' + key + '.tar', '-C', '/volume')
            restored[key] = inventory(volume, image) == manifest['volumes'][key]['entries']
            if not restored[key]:
                raise ValueError('Restored files differ: ' + key)
        print(json.dumps(dict(restored_stack=stack, all_entries_equal=restored)))
    else:
        raise ValueError('Expected backup or restore')


if __name__ == '__main__':
    os.umask(0o077)
    main()
