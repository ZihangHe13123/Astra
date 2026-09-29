"""Bounded, read-only OOXML package checks for saved deliverables.

Reopening the ZIP/XML package does not validate visual layout or recalculate formulas.
External relationships are reported and never fetched. No source bytes are written.
"""
from __future__ import annotations

import hashlib
import io
import os
import posixpath
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote
from zipfile import BadZipFile, ZipFile

MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 5000
MAIN_PARTS = {'.docx': 'word/document.xml', '.pptx': 'ppt/presentation.xml', '.xlsx': 'xl/workbook.xml'}


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit('}', 1)[-1]


def _relationship_target(name: str, target: str) -> str:
    target = unquote(target.split('#', 1)[0])
    base = '' if name == '_rels/.rels' else posixpath.dirname(posixpath.dirname(name))
    resolved = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(base, target))
    if not target or resolved.startswith('../') or '\\' in resolved or re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', resolved):
        raise ValueError(f'Invalid internal relationship in {name}')
    return resolved


def inspect_saved_document(path: Path) -> dict:
    """Caller must authorize the resolved path before calling, including reads outside a workspace."""
    suffix = path.suffix.lower()
    if suffix not in MAIN_PARTS:
        raise ValueError('doc_check supports saved .docx, .pptx and .xlsx files.')
    with path.open('rb') as source:
        before = os.fstat(source.fileno())
        if before.st_size > MAX_SOURCE_BYTES:
            raise ValueError('Document exceeds the 32 MiB inspection limit.')
        data = source.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError('Document exceeds the 32 MiB inspection limit.')
        if _identity(before) != _identity(os.fstat(source.fileno())) or _identity(before) != _identity(path.stat()):
            raise ValueError('Source changed while reading; check the current saved version again.')
    xml: dict[str, ET.Element] = {}
    try:
        with ZipFile(io.BytesIO(data)) as package:
            entries = package.infolist()
            if len(entries) > MAX_ENTRIES or sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES:
                raise ValueError('Document ZIP expansion exceeds inspection limits.')
            names = {item.filename for item in entries}
            if len(names) != len(entries):
                raise ValueError('Document ZIP contains duplicate parts.')
            for item in entries:
                name = item.filename
                if name.startswith('/') or '\\' in name or '..' in name.split('/') or item.flag_bits & 1:
                    raise ValueError('Document ZIP contains invalid paths or encrypted parts.')
                if not name.lower().endswith(('.xml', '.rels')):
                    continue
                if item.file_size > MAX_XML_BYTES:
                    raise ValueError('Document XML part exceeds the 16 MiB limit.')
                with package.open(item) as part:
                    raw = part.read(MAX_XML_BYTES + 1)
                if len(raw) > MAX_XML_BYTES:
                    raise ValueError('Document XML expansion exceeds inspection limits.')
                # Also recognizes UTF-16/32 markup before the standard XML parser runs.
                if re.search(rb'<!\s*(?:DOCTYPE|ENTITY)\b', raw.replace(b'\0', b''), re.I):
                    raise ValueError(f'DTD/entity declarations are not allowed: {name}')
                xml[name] = ET.fromstring(raw)
    except (BadZipFile, ET.ParseError, RuntimeError, NotImplementedError) as error:
        raise ValueError(f'Cannot reopen the document ZIP/XML: {error}') from error
    for required in ('[Content_Types].xml', '_rels/.rels', MAIN_PARTS[suffix]):
        if required not in xml:
            raise ValueError(f'Missing required document part: {required}')
    if _tag(xml['[Content_Types].xml']) != 'Types':
        raise ValueError('Invalid document content types.')
    expected = {'.docx': 'document', '.pptx': 'presentation', '.xlsx': 'workbook'}[suffix]
    if _tag(xml[MAIN_PARTS[suffix]]) != expected:
        raise ValueError('Invalid main document structure.')
    external = 0
    root_targets: set[str] = set()
    for name, document in xml.items():
        if not name.endswith('.rels'):
            continue
        if _tag(document) != 'Relationships':
            raise ValueError(f'Invalid package relationship part: {name}')
        for relation in document:
            if _tag(relation) != 'Relationship':
                continue
            if relation.get('TargetMode') == 'External':
                external += 1
                continue
            target = _relationship_target(name, relation.get('Target', ''))
            if target not in names:
                raise ValueError(f'Missing internal relationship target: {name} -> {target}')
            if name == '_rels/.rels':
                root_targets.add(target)
    if MAIN_PARTS[suffix] not in root_targets:
        raise ValueError('Package relationships do not reference the main document.')
    formulas = missing = 0
    if suffix == '.xlsx':
        for name, document in xml.items():
            if not name.startswith('xl/worksheets/'):
                continue
            for cell in document.iter():
                if _tag(cell) != 'c':
                    continue
                children = {_tag(child): child for child in cell}
                if 'f' in children:
                    formulas += 1
                    if 'v' not in children or (not children['v'].text and cell.get('t') != 'str'):
                        missing += 1
    if _identity(before) != _identity(path.stat()):
        raise ValueError('Source changed during inspection; check the current saved version again.')
    return {
        'path': str(path), 'format': suffix[1:], 'status': 'checked', 'source_bytes': len(data),
        'source_sha256': hashlib.sha256(data).hexdigest(), 'parts': len(entries),
        'checks': {'structure': 'passed', 'package_reopen': 'passed', 'application_reopen': 'not_run',
                   'layout': 'not_run', 'formula_recalculation': 'not_run'},
        'formulas': {'count': formulas, 'without_cached_value': missing},
        'external_relationships': external,
        'warnings': [
            'ZIP/XML structure checks do not prove visual layout or Office application compatibility.',
            *(['Formulas were not recalculated; saved caches may be missing or stale.'] if formulas else []),
            *(['External relationship targets were not accessed.'] if external else []),
        ],
    }
