"""Read-only discovery of the runtime shipped with this desktop release."""
from __future__ import annotations

import json
from pathlib import Path

from agent.launcher.common import LauncherError, read_json
from .registry import ToolDef, ToolRegistry


def workspace_dependencies(backend: Path | None = None) -> dict:
    root = (backend or Path(__file__).resolve().parents[3]).resolve()
    if not (root / 'desktop-installation.json').is_file():
        return {'status': 'unavailable', 'reason': 'This installation has no bundled workspace runtime.'}
    runtime = root.parent
    try:
        manifest = read_json(runtime / 'runtime.json')
        if manifest.get('schema') != 1 or manifest.get('distribution') != 'astra-desktop':
            raise LauncherError('Invalid desktop runtime descriptor.')

        def entry(name):
            value = manifest[name]
            if not isinstance(value, str) or Path(value).is_absolute():
                raise LauncherError('Invalid bundled dependency path.')
            path = (runtime / value).resolve(strict=True)
            if not path.is_relative_to(runtime):
                raise LauncherError('Bundled dependency escaped the runtime.')
            return str(path)

        return {'status': 'available', 'desktop_version': manifest['version'], 'target': manifest['target'],
                'python': entry('python'), 'python_version': manifest['python_version'],
                'python_library': entry('python_library'),
                'python_distributions': manifest['python_distributions'], 'office': manifest.get('office', {'available': False}),
                'read_only': True,
                'note': 'Use this interpreter to create or inspect artifacts. Structure checks do not establish visual layout or formula recalculation.'}
    except (OSError, ValueError, KeyError, LauncherError) as exc:
        return {'status': 'unavailable', 'reason': str(exc)}


def register_workspace_dependency_tools(registry: ToolRegistry) -> None:
    registry.register(ToolDef(
        name='load_workspace_dependencies',
        description='Read the bundled desktop Python interpreter, library directory and exact package versions for document, spreadsheet and PDF work. Returns explicit unavailable state on source installations. Does not install dependencies or modify the workspace.',
        parameters={'type': 'object', 'properties': {}, 'additionalProperties': False},
        fn=lambda: json.dumps(workspace_dependencies(), ensure_ascii=False),
        risk='read', approval='never', sandboxed=False, group='documents', strict_schema=True,
    ))
