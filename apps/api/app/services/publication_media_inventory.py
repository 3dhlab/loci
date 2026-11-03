"""Content identities for regular media beneath the configured media root.

Files and every directory component are opened without following links. Hashing
is bounded in memory; metadata/path changes during a read abort the inventory.
This records source bytes only. Destination receipt/readback is a separate gate.
"""
from contextlib import ExitStack, contextmanager
import gzip
import hashlib
import os
from pathlib import Path
import stat
import tarfile


class MediaInventoryError(RuntimeError):
    pass


def _path(value):
    if (not isinstance(value, str) or not value or value.startswith('/') or
            '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value) or
            any(c in ('', '.', '..') for c in value.split('/'))):
        raise MediaInventoryError('Published media path is invalid.')
    return value.split('/')


def _directory(stack, part, parent=None):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(part, flags, dir_fd=parent)
    stack.callback(os.close, fd)
    return fd


def _root(stack, root, identities=None):
    path = Path(os.path.abspath(root))
    fd = _directory(stack, '/')
    if identities is not None:identities.append(_identity(os.fstat(fd)))
    for part in path.parts[1:]:
        fd = _directory(stack, part, fd)
        if identities is not None:identities.append(_identity(os.fstat(fd)))
    return fd


def _parent(stack, root_fd, parts, identities=None):
    fd = root_fd
    for part in parts[:-1]:
        fd = _directory(stack, part, fd)
        if identities is not None:identities.append(_identity(os.fstat(fd)))
    return fd


def _identity(s):
    return (s.st_dev, s.st_ino)


def _version(s):
    return (_identity(s), s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


@contextmanager
def _stable_file(root_fd, root_identities, media_root, parts):
    with ExitStack() as opened:
        parent_identities=[]
        parent = _parent(opened, root_fd, parts,parent_identities)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        opened.callback(os.close, fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise MediaInventoryError('Published media is not a regular file.')
        yield fd, before
        if _version(os.fstat(fd)) != _version(before):
            raise MediaInventoryError('Published media changed during hashing.')
        current_parent_identities=[]
        current_parent = _parent(opened, root_fd, parts,current_parent_identities)
        current_file = os.stat(parts[-1], dir_fd=current_parent, follow_symlinks=False)
        current_root_identities=[]
        _root(opened, media_root,current_root_identities)
        if (current_parent_identities != parent_identities or
                current_root_identities != root_identities or
                _version(current_file) != _version(before)):
            raise MediaInventoryError('Published media path changed during hashing.')


def build_media_inventory(media_root, media_paths, progress=None):
    """Retain size/mtime compatibility and add SHA256 for every selected file.

    No transport, database, settings mutation or media writes occur. Duplicate
    or malformed names, links, nonregular files and unstable reads fail closed.
    Empty selections require no media directory, matching record-only exports.
    """
    try:
        paths = list(media_paths)
        if not paths:
            return {}
        parsed = [(value, _path(value)) for value in paths]
        if len({value for value, _ in parsed}) != len(parsed):
            raise MediaInventoryError('Published media path is duplicated.')
        inventory = {}
        with ExitStack() as roots:
            root_identities=[]
            root_fd = _root(roots, media_root,root_identities)
            for index, (value, parts) in enumerate(parsed,start=1):
                with _stable_file(root_fd, root_identities, media_root, parts) as (fd, before):
                    digest = hashlib.sha256()
                    while True:
                        chunk = os.read(fd, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                inventory[value] = {'size_bytes':before.st_size,
                                    'mtime_ns':before.st_mtime_ns,
                                    'sha256':digest.hexdigest()}
                if progress:
                    progress(index,len(parsed),Path(value).name)
        return inventory
    except MediaInventoryError:
        raise
    except (OSError, TypeError, ValueError):
        # Avoid exposing host paths, private names or operating-system details.
        raise MediaInventoryError('Published media is unavailable or changed.') from None


class _HashingReader:
    def __init__(self, fd):
        self.fd=fd;self.digest=hashlib.sha256();self.size=0

    def read(self, size):
        chunks=[];remaining=size
        while remaining:
            data=os.read(self.fd,min(remaining,1024*1024))
            if not data:break
            self.digest.update(data);self.size+=len(data)
            chunks.append(data);remaining-=len(data)
        return b''.join(chunks)


def create_verified_media_bundle(bundle_path, media_root, media_paths, expected_inventory,
                                 progress=None):
    """Write a local tar only from bytes matching the previously captured hashes.

    Callers must upload only after successful return. A failed/partial output is
    retained locally for the caller's temporary-directory cleanup, never passed
    to transport here. An existing destination is never overwritten.
    """
    try:
        parsed=[(value,_path(value)) for value in media_paths]
        if not isinstance(expected_inventory,dict):
            raise MediaInventoryError('Published media inventory is missing.')
        if len({value for value,_ in parsed}) != len(parsed):
            raise MediaInventoryError('Published media path is duplicated.')
        with ExitStack() as roots:
            root_identities=[]
            root_fd=_root(roots,media_root,root_identities) if parsed else None
            with open(bundle_path,'xb') as output, gzip.GzipFile(filename='',mode='wb',fileobj=output,mtime=0) as compressed, tarfile.open(fileobj=compressed,mode='w|') as archive:
                for index,(value,parts) in enumerate(parsed,start=1):
                    expected=expected_inventory.get(value)
                    if not isinstance(expected,dict) or not isinstance(expected.get('sha256'),str):
                        raise MediaInventoryError('Published media content identity is missing.')
                    with _stable_file(root_fd,root_identities,media_root,parts) as (fd,before):
                        if expected.get('size_bytes') != before.st_size or expected.get('mtime_ns') != before.st_mtime_ns:
                            raise MediaInventoryError('Published media changed after inventory.')
                        item=tarfile.TarInfo(value);item.size=before.st_size;item.mode=0o644
                        item.uid=item.gid=0;item.mtime=before.st_mtime
                        reader=_HashingReader(fd);archive.addfile(item,reader)
                        if reader.size != before.st_size or reader.digest.hexdigest() != expected['sha256']:
                            raise MediaInventoryError('Published media content changed after inventory.')
                    if progress:
                        progress(index,len(parsed),Path(value).name)
    except MediaInventoryError:
        raise
    except (OSError, TypeError, ValueError, tarfile.TarError):
        raise MediaInventoryError('Published media bundle is unavailable or changed.') from None
