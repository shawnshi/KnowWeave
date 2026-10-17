"""Private patch artifacts anchored against directory replacement.

Windows pins every ancestor without FILE_SHARE_DELETE and rejects reparse points;
POSIX traverses/creates via directory descriptors with O_NOFOLLOW. Neither backend
excludes Wiki editors or grants live-application authority. Unsupported capabilities
fail closed. Artifact names/digests remain snapshots, not authentication.

Win32 contracts: Microsoft Learn CreateFileW, GetFileInformationByHandle, WriteFile.
"""

from __future__ import annotations

import os
import uuid
from contextlib import ExitStack
from pathlib import Path


class ArtifactBundle:
    def __init__(self, wiki: Path):
        self.wiki = wiki.resolve()
        self.path = (
            self.wiki
            / ".meta"
            / "source_metadata_patches"
            / ("patch-" + uuid.uuid4().hex)
        )
        self._stack = ExitStack()
        self._fd = None
        self._kernel = None
        self._active = False

    def __enter__(self):
        try:
            if os.name == "nt":
                self._enter_windows()
            elif (
                os.name == "posix"
                and hasattr(os, "O_NOFOLLOW")
                and hasattr(os, "O_DIRECTORY")
                and all(fn in os.supports_dir_fd for fn in (os.open, os.mkdir))
            ):
                self._enter_posix()
            else:
                raise RuntimeError(
                    "Safe source patch artifact creation is unsupported on this platform"
                )
            self._active = True
            return self
        except BaseException:
            self._stack.close()
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        self._active = False
        self._stack.close()

    def _enter_posix(self):
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW")
        fd = os.open(self.wiki.anchor, flags)
        self._stack.callback(os.close, fd)
        for component in self.wiki.parts[1:]:
            fd = os.open(component, flags, dir_fd=fd)
            self._stack.callback(os.close, fd)
        for component in (".meta", "source_metadata_patches", self.path.name):
            try:
                os.mkdir(component, mode=0o700, dir_fd=fd)
            except FileExistsError:
                if component == self.path.name:
                    raise
            fd = os.open(component, flags, dir_fd=fd)
            self._stack.callback(os.close, fd)
        self._fd = fd

    def _enter_windows(self):
        import ctypes
        from ctypes import wintypes

        if not self.wiki.drive or self.wiki.anchor.startswith("\\\\"):
            raise RuntimeError("Safe Windows patch artifacts require a local drive")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel.GetDriveTypeW.restype = wintypes.UINT
        kernel.WriteFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        kernel.WriteFile.restype = wintypes.BOOL
        kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        kernel.FlushFileBuffers.restype = wintypes.BOOL

        class Information(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("created", wintypes.FILETIME),
                ("accessed", wintypes.FILETIME),
                ("written", wintypes.FILETIME),
                ("volume", wintypes.DWORD),
                ("size_hi", wintypes.DWORD),
                ("size_lo", wintypes.DWORD),
                ("links", wintypes.DWORD),
                ("index_hi", wintypes.DWORD),
                ("index_lo", wintypes.DWORD),
            ]

        kernel.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Information),
        ]
        kernel.GetFileInformationByHandle.restype = wintypes.BOOL
        if kernel.GetDriveTypeW(self.wiki.anchor) != 3:
            raise RuntimeError(
                "Safe Windows patch artifacts require a fixed local drive"
            )
        self._kernel = kernel
        self._information = Information
        self._invalid_handle = ctypes.c_void_p(-1).value
        current = Path(self.wiki.anchor)
        self._pin_directory(current)
        for component in self.wiki.parts[1:]:
            current = current / component
            self._pin_directory(current)
        for component in (".meta", "source_metadata_patches", self.path.name):
            current = current / component
            current.mkdir(mode=0o700, exist_ok=component != self.path.name)
            self._pin_directory(current)

    def _pin_directory(self, path):
        import ctypes

        # FILE_LIST_DIRECTORY|FILE_READ_ATTRIBUTES; READ|WRITE sharing, no DELETE.
        # Attributes-only handles do NOT enforce Windows delete-sharing exclusion.
        # OPEN_EXISTING; BACKUP_SEMANTICS|OPEN_REPARSE_POINT, never follow links.
        kernel = self._kernel
        if kernel is None:
            raise RuntimeError("Windows directory capability is not initialized")
        handle = kernel.CreateFileW(str(path), 0x81, 0x3, None, 3, 0x02200000, None)
        if handle is None or handle == self._invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._stack.callback(kernel.CloseHandle, handle)
        information = self._information()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        if information.attributes & 0x400 or not information.attributes & 0x10:
            raise ValueError(
                "Source patch artifact boundary contains a reparse point or non-directory"
            )

    def write_new(self, name: str, raw: bytes):
        if not self._active:
            raise RuntimeError("Artifact directory capability is not active")
        if not name or name in {".", ".."} or any(c in name for c in ("/", "\\", ":")):
            raise ValueError("Artifact leaf name is not a plain filename")
        if self._fd is not None:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW"),
                0o600,
                dir_fd=self._fd,
            )
            with os.fdopen(fd, "wb") as file:
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            return
        if self._kernel is None:
            raise RuntimeError("Artifact directory capability is not active")
        import ctypes
        from ctypes import wintypes

        # GENERIC_WRITE; CREATE_NEW; OPEN_REPARSE_POINT. A pre-existing leaf fails.
        handle = self._kernel.CreateFileW(
            str(self.path / name), 0x40000000, 0, None, 1, 0x00200000, None
        )
        if handle is None or handle == self._invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            written = wintypes.DWORD()
            data = ctypes.create_string_buffer(raw)
            if not self._kernel.WriteFile(
                handle, data, len(raw), ctypes.byref(written), None
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if written.value != len(raw):
                raise OSError("Short source patch artifact write")
            if not self._kernel.FlushFileBuffers(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._kernel.CloseHandle(handle)
