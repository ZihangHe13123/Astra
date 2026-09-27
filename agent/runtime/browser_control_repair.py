"""Explicit, conservative migration of old Windows Browser Control storage."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sys

from . import browser_control_windows as windows


def read_owned(path, limit=16384):
    with os.fdopen(windows.open_file(path, os.O_RDONLY, permissions=False), 'rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Oversized Browser Control recovery metadata')
    return data


def check_owned(path, *, directory=False):
    with windows.handle(path) as value:
        windows.validate_handle(value, directory=directory, permissions=False)


@contextmanager
def existing_lock(path):
    import msvcrt
    fd = windows.open_file(path, os.O_RDWR, permissions=False)
    try:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('Browser Control is in use; stop the owning runtime before repair') from exc
        yield
    finally:
        os.close(fd)


def repair_host_permissions(paths):
    from .browser_control_install import BROWSERS, HOST_NAME, _content
    directory = paths['launcher'].parent
    check_owned(directory, directory=True)
    children = set(directory.iterdir())
    allowed = {directory / 'management.lock'}
    verified = set()
    for browser in BROWSERS:
        peer = {'manifest': directory / f'{browser}-{HOST_NAME}.json',
                'launcher': directory / f'host-{browser}.cmd',
                'record': directory / f'owned-{browser}.json'}
        allowed.update(peer.values())
        if peer['record'] not in children:
            if children.intersection(peer.values()):
                raise PermissionError('Incomplete ownership record; manual recovery is required')
            continue
        record = json.loads(read_owned(peer['record']))
        if not isinstance(record, dict) or not re.fullmatch('[a-p]{32}', str(record.get('extension_id', ''))):
            raise ValueError('Invalid recovery ownership record')
        if not all(isinstance(record.get(key), str) and Path(record[key]).is_absolute() for key in ('repo', 'python')):
            raise ValueError('Invalid recovery paths')
        expected = _content(peer, browser, record['extension_id'], Path(record['repo']), Path(record['python']))
        if record != json.loads(expected['record']):
            raise PermissionError('Unrecognized recovery generator or ownership data')
        for key, path in peer.items():
            if path in children:
                if read_owned(path) != expected[key]:
                    raise PermissionError('Changed artifacts were preserved; manual recovery is required')
                verified.add(path)
    if paths['record'] not in verified or children - allowed:
        raise PermissionError('Foreign files were preserved; refusing directory permission repair')
    for path in children:
        check_owned(path)
    with existing_lock(directory / 'management.lock'):
        windows.tighten(directory, directory=True)
        for path in children:
            windows.tighten(path)


def repair_endpoint_permissions(directory):
    """Never take over an active endpoint, change contents, or print its token."""
    from .browser_control_transport import OWNER_INFO, RELEASE_REQUEST
    if not directory.exists():
        return
    check_owned(directory, directory=True)
    children = set(directory.iterdir())
    lock, descriptor = directory / 'owner.lock', directory / 'endpoint.json'
    owner, request = directory / OWNER_INFO, directory / RELEASE_REQUEST
    if children - {lock, descriptor, owner, request}:
        raise PermissionError('Unexpected endpoint files; manual recovery is required')
    for path in children:
        check_owned(path)
    if descriptor in children:
        data = json.loads(read_owned(descriptor, 4096))
        if (not isinstance(data, dict) or set(data) != {'port', 'pid', 'token'}
                or type(data['port']) is not int or not 0 < data['port'] < 65536
                or type(data['pid']) is not int or not 0 < data['pid'] <= 2**31 - 1
                or not isinstance(data['token'], str) or not 32 <= len(data['token']) <= 128):
            raise ValueError('Unexpected endpoint metadata; manual recovery is required')
    for path, fields in ((owner, {'pid', 'label'}), (request, {'pid', 'label', 'at'})):
        if path not in children:
            continue
        data = json.loads(read_owned(path, 4096))
        if (not isinstance(data, dict) or set(data) != fields
                or type(data['pid']) is not int or not 0 < data['pid'] <= 2**31 - 1
                or not isinstance(data['label'], str) or len(data['label']) > 60):
            raise ValueError('Unexpected handover metadata; manual recovery is required')
        if path == request and (type(data['at']) not in (int, float)
                                or not 0 <= data['at'] <= sys.float_info.max):
            raise ValueError('Unexpected handover timestamp; manual recovery is required')
    if lock not in children:
        if children:
            raise PermissionError('Endpoint has no ownership lock; manual recovery is required')
        windows.tighten(directory, directory=True)
        return
    with existing_lock(lock):
        windows.tighten(directory, directory=True)
        for path in children:
            windows.tighten(path)
