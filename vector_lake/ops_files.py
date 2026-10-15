"""Local public telemetry IO with held directory handles and no link traversal."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    _kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL

    class _Attributes(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('tag', wintypes.DWORD)]

    def _windows_open(path, directory=False):
        flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT / BACKUP_SEMANTICS
        access = 0x80000000
        handle = _kernel.CreateFileW(str(path), access, 3, None, 3, flags, None)  # no FILE_SHARE_DELETE
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = _Attributes()
        if not _kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            error = ctypes.WinError(ctypes.get_last_error())
            _kernel.CloseHandle(handle)
            raise error
        if attributes.attributes & 0x400 or directory and not attributes.attributes & 0x10:
            _kernel.CloseHandle(handle)
            raise OSError('telemetry link or non-directory rejected')
        return handle


@contextmanager
def directory(path):
    """Hold every ancestor against Windows replacement; POSIX uses no-follow dirfd."""
    path = Path(path).expanduser().absolute()
    if '..' in path.parts or not path.anchor:
        raise ValueError('explicit local telemetry directory required')
    handles = []
    try:
        if os.name == 'nt':
            if path.anchor.startswith('\\\\'):
                raise OSError('remote telemetry roots unsupported')
            current = Path(path.anchor)
            handles.append(_windows_open(current, directory=True))
            for part in path.parts[1:]:
                current = current / part
                handles.append(_windows_open(current, directory=True))
            yield path
        else:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            handles.append(os.open(path.anchor, flags))
            for part in path.parts[1:]:
                handles.append(os.open(part, flags, dir_fd=handles[-1]))
            yield handles[-1]
    finally:
        for handle in reversed(handles):
            if os.name == 'nt':
                _kernel.CloseHandle(handle)
            else:
                os.close(handle)


def mkdir_child(parent, name):
    try:
        if os.name == 'nt':
            (parent / name).mkdir()
        else:
            os.mkdir(name, dir_fd=parent)
    except FileExistsError:
        pass  # directory() validates before use


def read_public(path, limit):
    path = Path(path).absolute()
    with directory(path.parent) as parent:
        if os.name == 'nt':
            import msvcrt
            handle = _windows_open(path)
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
            except BaseException:
                _kernel.CloseHandle(handle)
                raise
        else:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError('public heartbeat is not a regular file')
            return stream.read(limit)


def atomic_public(parent, filename, temporary, data):
    if os.name == 'nt':
        target, tmp = parent / filename, parent / temporary
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    else:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == 'nt':
            os.replace(tmp, target)
        else:
            os.replace(temporary, filename, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            if os.name == 'nt':
                tmp.unlink()
            else:
                os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
