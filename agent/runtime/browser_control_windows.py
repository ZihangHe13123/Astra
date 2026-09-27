"""Small Win32 ACL adapter for Browser Control (no optional native runtime)."""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from contextlib import contextmanager
from functools import lru_cache
import os
from pathlib import Path


class SecurityAttributes(c.Structure):
    _fields_ = [('length', w.DWORD), ('descriptor', c.c_void_p), ('inherit', w.BOOL)]


class FileInformation(c.Structure):
    _fields_ = [('attributes', w.DWORD), ('created', w.FILETIME),
                ('accessed', w.FILETIME), ('written', w.FILETIME),
                ('volume', w.DWORD), ('size_high', w.DWORD), ('size_low', w.DWORD),
                ('links', w.DWORD), ('index_high', w.DWORD), ('index_low', w.DWORD)]


class Acl(c.Structure):
    _fields_ = [('revision', w.BYTE), ('reserved', w.BYTE), ('size', w.WORD),
                ('count', w.WORD), ('reserved2', w.WORD)]


@lru_cache(maxsize=1)
def api():
    kernel = c.WinDLL('kernel32', use_last_error=True)
    advapi = c.WinDLL('advapi32', use_last_error=True)
    signatures = [
        (kernel, 'CreateFileW', [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
        (kernel, 'CreateDirectoryW', [w.LPCWSTR, c.c_void_p], w.BOOL),
        (kernel, 'GetFileInformationByHandle', [w.HANDLE, c.POINTER(FileInformation)], w.BOOL),
        (kernel, 'CloseHandle', [w.HANDLE], w.BOOL),
        (kernel, 'GetCurrentProcess', [], w.HANDLE),
        (kernel, 'LocalFree', [c.c_void_p], c.c_void_p),
        (advapi, 'OpenProcessToken', [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)], w.BOOL),
        (advapi, 'GetTokenInformation', [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)], w.BOOL),
        (advapi, 'ConvertSidToStringSidW', [c.c_void_p, c.POINTER(c.c_void_p)], w.BOOL),
        (advapi, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p], w.BOOL),
        (advapi, 'GetSecurityInfo', [w.HANDLE, c.c_int, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p], w.DWORD),
        (advapi, 'GetSecurityDescriptorControl', [c.c_void_p, c.POINTER(w.WORD), c.POINTER(w.DWORD)], w.BOOL),
        (advapi, 'GetSecurityDescriptorDacl', [c.c_void_p, c.POINTER(w.BOOL), c.POINTER(c.c_void_p), c.POINTER(w.BOOL)], w.BOOL),
        (advapi, 'GetAce', [c.c_void_p, w.DWORD, c.POINTER(c.c_void_p)], w.BOOL),
        (advapi, 'SetSecurityInfo', [w.HANDLE, c.c_int, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p], w.DWORD),
    ]
    for dll, name, args, result in signatures:
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, result
    return kernel, advapi


def checked(result):
    if not result:
        raise c.WinError(c.get_last_error())
    return result


def sid_string(sid):
    kernel, advapi = api()
    value = c.c_void_p()
    checked(advapi.ConvertSidToStringSidW(sid, c.byref(value)))
    try:
        return c.wstring_at(value)
    finally:
        kernel.LocalFree(value)


@lru_cache(maxsize=1)
def user_sid():
    kernel, advapi = api()
    token, length = w.HANDLE(), w.DWORD()
    checked(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, c.byref(token)))
    try:
        advapi.GetTokenInformation(token, 1, None, 0, c.byref(length))
        buffer = c.create_string_buffer(length.value)
        checked(advapi.GetTokenInformation(token, 1, buffer, length, c.byref(length)))
        return sid_string(c.cast(buffer, c.POINTER(c.c_void_p))[0])
    finally:
        kernel.CloseHandle(token)


@contextmanager
def security_attributes():
    kernel, advapi = api()
    descriptor = c.c_void_p()
    sid = user_sid()
    sddl = f'O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)'
    checked(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None))
    try:
        yield SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
    finally:
        kernel.LocalFree(descriptor)


def reject_reparse_parents(path):
    # Do not follow junctions, including those above the private directory.
    for parent in (Path(path).absolute(), *Path(path).absolute().parents):
        try:
            if parent.lstat().st_file_attributes & 0x400:
                raise PermissionError('Browser Control paths must not contain reparse points')
        except FileNotFoundError:
            continue


@contextmanager
def handle(path, *, access=0x20080, disposition=3, attributes=None):
    kernel, _ = api()
    reject_reparse_parents(path)
    value = kernel.CreateFileW(str(Path(path).absolute()), access, 3,
                               c.byref(attributes) if attributes else None,
                               disposition, 0x02200000, None)
    if value == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        yield value
    finally:
        kernel.CloseHandle(value)


def validate_handle(value, *, directory=False, permissions=True):
    kernel, advapi = api()
    info = FileInformation()
    checked(kernel.GetFileInformationByHandle(value, c.byref(info)))
    if bool(info.attributes & 0x10) != directory or info.attributes & 0x400 or (not directory and info.links != 1):
        raise PermissionError('Unsafe Browser Control file type or link')
    owner, acl, descriptor = c.c_void_p(), c.c_void_p(), c.c_void_p()
    result = advapi.GetSecurityInfo(value, 1, 5, c.byref(owner), None, c.byref(acl), None, c.byref(descriptor))
    if result:
        raise c.WinError(result)
    try:
        if sid_string(owner) != user_sid():
            raise PermissionError('Browser Control storage must be owned by the current user')
        if not permissions:
            return
        control, revision = w.WORD(), w.DWORD()
        checked(advapi.GetSecurityDescriptorControl(descriptor, c.byref(control), c.byref(revision)))
        if not acl or not control.value & 0x1000:
            raise PermissionError('Browser Control storage requires a protected private DACL; run browser-control repair')
        count = c.cast(acl, c.POINTER(Acl)).contents.count
        user_access = False
        for index in range(count):
            ace = c.c_void_p()
            checked(advapi.GetAce(acl, index, c.byref(ace)))
            if ace.value is None:
                raise PermissionError('Invalid Browser Control access rule')
            header = c.string_at(ace, 4)
            if header[0] != 0 or header[1] & 8:
                raise PermissionError('Unsupported Browser Control access rule')
            sid = sid_string(ace.value + 8)
            if sid not in {user_sid(), 'S-1-5-18'}:
                raise PermissionError('Browser Control storage permits another principal')
            mask = c.cast(ace.value + 4, c.POINTER(w.DWORD)).contents.value
            user_access |= sid == user_sid() and (mask & 0x1F01FF == 0x1F01FF or bool(mask & 0x10000000))
        if not user_access:
            raise PermissionError('Browser Control storage must grant its owner full access')
    finally:
        kernel.LocalFree(descriptor)


def check_directory(path):
    with handle(path) as value:
        validate_handle(value, directory=True)


def make_directory(path):
    path = Path(path)
    reject_reparse_parents(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    kernel, _ = api()
    with security_attributes() as attributes:
        if not kernel.CreateDirectoryW(str(path.absolute()), c.byref(attributes)):
            error = c.get_last_error()
            if error != 183:
                raise c.WinError(error)
    check_directory(path)


def open_file(path, flags, *, permissions=True):
    import msvcrt
    kernel, _ = api()
    access = 0x80000000 if not flags & (os.O_WRONLY | os.O_RDWR) else 0xC0000000
    disposition = 1 if flags & os.O_EXCL else 4 if flags & os.O_CREAT else 3
    reject_reparse_parents(path)
    with security_attributes() as attributes:
        value = kernel.CreateFileW(str(Path(path).absolute()), access, 3, c.byref(attributes),
                                   disposition, 0x00200000, None)
    if value == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        validate_handle(value, permissions=permissions)
        fd = msvcrt.open_osfhandle(value, (flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)) | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(value)
        raise
    return fd


def tighten(path, *, directory=False):
    """Only used by explicit repair after all owned artifacts are verified."""
    _, advapi = api()
    with handle(path, access=0x60080) as value:
        validate_handle(value, directory=directory, permissions=False)
        with security_attributes() as attributes:
            present, defaulted, acl = w.BOOL(), w.BOOL(), c.c_void_p()
            checked(advapi.GetSecurityDescriptorDacl(attributes.descriptor, c.byref(present), c.byref(acl), c.byref(defaulted)))
            result = advapi.SetSecurityInfo(value, 1, 0x80000004, None, None, acl, None)
            if result:
                raise c.WinError(result)
        validate_handle(value, directory=directory)
