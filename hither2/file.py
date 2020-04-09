from os import stat
from os.path import basename
import kachery as ka

class File:
    def __init__(self, path, item_type='file'):
        if path.startswith('sha1://') or path.startswith('sha1dir://'):
            self._sha1_path = path
        else:
            self._sha1_path = ka.store_file(path, basename=_get_basename_from_path(path))
        self.path = self._sha1_path
        self._item_type = item_type
    def serialize(self):
        ret = dict(
            _type='hither2_file',
            sha1_path=self._sha1_path,
            item_type=self._item_type
        )
        return ret
    def array(self):
        if self._item_type != 'ndarray':
            raise Exception('This file is not of type ndarray')
        x = ka.load_npy(self._sha1_path)
        if x is None:
            raise Exception(f'Unable to load npy file: {self._sha1_path}')
        return x
    @staticmethod
    def can_deserialize(x):
        if type(x) != dict:
            return False
        return (x.get('_type', None) == 'hither2_file') and ('sha1_path' in x)
    @staticmethod
    def deserialize(x):
        return File(x['sha1_path'], item_type=x.get('item_type', 'file'))

def _get_basename_from_path(path):
    if path.startswith('sha1://'):
        return _get_basename_from_path(path[7:])
    elif path.startswith('sha1dir://'):
        return _get_basename_from_path(path[10:])
    a = path.split('/')
    if len(a) > 1:
        return a[-1]
    return None
