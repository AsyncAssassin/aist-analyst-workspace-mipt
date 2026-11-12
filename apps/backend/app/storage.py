import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import unicodedata
import uuid
from .document_process import document_command
from .errors import ApiError

MAX_BYTES = 16 * 1024 * 1024


def safe_filename(value):
    value = unicodedata.normalize('NFC', value or '').replace('\\', '/').split('/')[-1]
    value = ''.join(c for c in value if not unicodedata.category(c).startswith('C'))
    value = value.strip(' .')[:240]
    return value or 'source.pdf'


def process_pdf(data):
    try:
        result = document_command([sys.executable, '-m', 'app.pdf_worker'], seconds=40, data=data,
                                  python_path=Path(__file__).resolve().parents[1])
        payload = json.loads(result)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        raise ApiError('too_large') from None
    except (ValueError, OSError):
        raise ApiError('temporarily_unavailable') from None
    if 'error' in payload:
        raise ApiError(payload['error'])
    return payload


def file_identity(info):
    return info.st_dev, info.st_ino


def file_stamp(info):
    return file_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns


class Storage:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError('Storage directory cannot be a symlink')

    def path(self, name):
        if not re.fullmatch(r'sv_[a-f0-9]{32}\.pdf', name):
            raise ApiError('temporarily_unavailable')
        return self.root / name

    def read(self, version):
        try:
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return None, 'temporarily_unavailable'
        try:
            fd = os.open(self.path(version['storage_name']).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return None, 'export_corrupt'
                data = stream.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return None, 'export_missing'
        except OSError as error:
            if error.errno == errno.ELOOP:
                return None, 'export_corrupt'
            return None, 'temporarily_unavailable'
        finally:
            os.close(directory)
        if len(data) != version['byte_size'] or hashlib.sha256(data).hexdigest() != version['sha256']:
            return None, 'export_corrupt'
        return data, None

    def publish(self, version_id, data):
        final = self.path(version_id + '.pdf')
        temporary = self.root / ('.' + version_id + '.' + uuid.uuid4().hex + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        identity = os.fstat(fd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                written = os.fstat(stream.fileno())
            self.verify_candidate(temporary, written)
            # link не перезаписывает чужой или ранее опубликованный файл.
            os.link(temporary, final, follow_symlinks=False)
            self.unlink_owned(temporary, identity)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            with os.fdopen(os.open(final, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
                saved = os.fstat(stream.fileno())
                if (not stat.S_ISREG(saved.st_mode) or file_identity(saved) != file_identity(identity)
                        or stream.read(MAX_BYTES + 1) != data):
                    raise OSError('Published PDF differs from the prepared file')
            self.verify_candidate(final, saved)
        except BaseException:
            self.remove_owned(temporary, identity, data, partial=True)
            self.remove_owned(final, identity, data)
            raise
        return final, saved

    def verify_candidate(self, path, saved):
        current = path.lstat()
        if not stat.S_ISREG(current.st_mode) or file_stamp(current) != file_stamp(saved):
            raise OSError('Prepared PDF changed before commit')

    def unlink_owned(self, path, identity):
        current = path.lstat()
        if not stat.S_ISREG(current.st_mode) or file_identity(current) != file_identity(identity):
            raise OSError('Upload path no longer refers to the owned file')
        path.unlink()

    def remove_owned(self, path, identity, data, partial=False):
        try:
            current = path.lstat()
            if not stat.S_ISREG(current.st_mode) or file_identity(current) != file_identity(identity):
                return False
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if file_identity(opened) != file_identity(identity):
                    return False
                saved = stream.read(MAX_BYTES+1)
                if partial:
                    if not path.name.startswith('.') or not data.startswith(saved):
                        return False
                elif hashlib.sha256(saved).digest() != hashlib.sha256(data).digest():
                    return False
            self.unlink_owned(path, identity)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False
