"""Check export hard links and reject archive paths outside the restored volume."""
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from d17_backup import check_archive


class BackupArchiveTest(unittest.TestCase):
    def check_members(self, members):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'exports.tar'
            with tarfile.open(archive, 'w') as stream:
                for name, kind, target in members:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = target
                    stream.addfile(member, io.BytesIO(b''))
            check_archive(archive)

    def test_export_hard_link_to_prior_file(self):
        self.check_members([
            ('./.export/note.pdf', tarfile.REGTYPE, ''),
            ('./export.pdf', tarfile.LNKTYPE, './.export/note.pdf'),
        ])

    def test_escaping_file_path(self):
        for name in ('/outside', '../outside', 'nested/../../outside'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.check_members([(name, tarfile.REGTYPE, '')])

    def test_hard_link_requires_prior_regular_file(self):
        for target in ('/outside', '../outside', 'missing.pdf'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.check_members([('export.pdf', tarfile.LNKTYPE, target)])

    def test_symlink_and_special_file_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.check_members([('unsafe', kind, 'target')])


if __name__ == '__main__':
    unittest.main()
