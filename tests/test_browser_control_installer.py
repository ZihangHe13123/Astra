import json
import os
from pathlib import Path
import pytest
from scripts.install_browser_control_host import install, uninstall, HOST_NAME

@pytest.mark.skipif(os.name != 'posix', reason='macOS native host launcher requires POSIX ownership and modes')
def test_exact_manifest_private_launcher_and_uninstall(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.install_browser_control_host.sys.platform", "darwin")
    repo=Path(__file__).resolve().parents[1]
    paths=install(extension_id='a'*32,browser='edge',home=tmp_path,repo=repo,python=Path(os.sys.executable))
    manifest=json.loads(paths['manifest'].read_text())
    assert manifest['name']==HOST_NAME
    assert manifest['allowed_origins']==['chrome-extension://'+'a'*32+'/']
    assert Path(manifest['path']).stat().st_mode & 0o777 == 0o700
    assert str(repo) in paths['launcher'].read_text()
    original=paths['manifest'].read_bytes()
    with pytest.raises(FileExistsError): install(extension_id='b'*32,browser='edge',home=tmp_path,repo=repo,python=Path(os.sys.executable))
    assert paths['manifest'].read_bytes()==original
    uninstall(browser='edge',home=tmp_path)
    assert not paths['manifest'].exists() and not paths['launcher'].exists()

@pytest.mark.parametrize('extension_id',['*','a'*31,'z'*32,'a'*32+'/'])
def test_invalid_extension_id_never_writes(tmp_path,extension_id):
    with pytest.raises(ValueError): install(extension_id=extension_id,browser='edge',home=tmp_path)
    assert list(tmp_path.iterdir())==[]

@pytest.mark.skipif(os.name != 'posix', reason='macOS native host launcher requires POSIX ownership and modes')
def test_install_rolls_back_launcher_if_manifest_write_fails(tmp_path,monkeypatch):
    import scripts.install_browser_control_host as installer
    monkeypatch.setattr(installer.sys,'platform','darwin')
    original=installer.os.open
    def fail_manifest(path,*args,**kwargs):
        if str(path).endswith('.json'): raise PermissionError('injected manifest failure')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(installer.os,'open',fail_manifest)
    with pytest.raises(PermissionError):
        install(extension_id='a'*32,browser='edge',home=tmp_path)
    assert not list(tmp_path.rglob('host-edge.sh'))
    assert not list(tmp_path.rglob(HOST_NAME+'.json'))


def test_install_rejects_unsupported_platform_before_creating_files(tmp_path, monkeypatch):
    monkeypatch.setattr('scripts.install_browser_control_host.sys.platform', 'linux')
    with pytest.raises(RuntimeError, match='Windows and macOS'):
        install(extension_id='a' * 32, browser='edge', home=tmp_path)
    assert list(tmp_path.iterdir()) == []
