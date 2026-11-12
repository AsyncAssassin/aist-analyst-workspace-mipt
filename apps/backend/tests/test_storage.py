import os
from unittest.mock import patch
import pytest
from app.storage import Storage


def test_partial_write_cleans_own_file_preserves_foreign(tmp_path):
    store=Storage(tmp_path)
    foreign=tmp_path/'foreign.pdf';foreign.write_bytes(b'foreign')
    real_fdopen=os.fdopen
    class PartialWrite:
        def __init__(self,stream):self.stream=stream
        def __enter__(self):return self
        def __exit__(self,*args):self.stream.close()
        def write(self,content):
            self.stream.write(content[:3]);self.stream.flush()
            raise OSError('partial write')
    def fdopen(fd,mode,*args,**kwargs):
        stream=real_fdopen(fd,mode,*args,**kwargs)
        return PartialWrite(stream) if mode=='wb' else stream
    with patch('app.storage.os.fdopen',side_effect=fdopen),pytest.raises(OSError):
        store.publish('sv_'+'1'*32,b'full content')
    assert list(tmp_path.iterdir())==[foreign]
    assert foreign.read_bytes()==b'foreign'
