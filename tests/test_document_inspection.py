"""Read-only saved Office checks keep structural, visual, and formula evidence distinct."""
import asyncio
import json
import shutil
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from agent.runtime.tools.documents import register_document_tools
from agent.runtime.tools.files import register_file_tools
from agent.runtime.tools.registry import ToolRegistry

FIXTURES = Path(__file__).parents[1] / 'ui-gui/tests/fixtures/documents'

def registry(workspace):
    tools=ToolRegistry()
    register_file_tools(tools, workdir=str(workspace))
    register_document_tools(tools)
    return tools

def check(tools, path):
    result=asyncio.run(tools.execute('doc_check', {'path':str(path)}))
    return result if result.get('error') else json.loads(result['output'])

def test_doc_check_reads_office_source_without_claiming_layout_or_recalculation(tmp_path):
    tools=registry(tmp_path)
    for extension in ('docx', 'pptx', 'xlsx'):
        target=tmp_path/f'preview.{extension}'
        shutil.copyfile(FIXTURES/target.name,target)
        before=target.read_bytes()
        result=check(tools,target)
        assert not result.get('error'), result
        assert result['checks']['structure']=='passed'
        assert result['checks']['layout']=='not_run'
        assert result['checks']['formula_recalculation']=='not_run'
        assert len(result['source_sha256'])==64
        assert target.read_bytes()==before
        if extension=='xlsx':
            assert result['formulas']['count']==1
            assert result['formulas']['without_cached_value']==1

def test_doc_check_retains_file_permission_boundary(tmp_path):
    workspace=tmp_path/'inside';workspace.mkdir()
    target=tmp_path/'outside.docx';shutil.copyfile(FIXTURES/'preview.docx',target)
    result=check(registry(workspace),target)
    assert result.get('error'), result
    assert 'permission' in json.dumps(result).lower() or 'approv' in json.dumps(result).lower()

def test_doc_check_rejects_malformed_xml_and_dangling_relationships(tmp_path):
    tools=registry(tmp_path)
    for target_name, xml in [('broken.docx','<document><broken></document>'),('dangling.docx','<document/>')]:
        target=tmp_path/target_name
        with ZipFile(target,'w',ZIP_DEFLATED) as package:
            package.writestr('[Content_Types].xml','<Types/>')
            package.writestr('word/document.xml',xml)
            package.writestr('_rels/.rels','<Relationships><Relationship Id="rId1" Target="missing.xml"/></Relationships>')
        assert check(tools,target).get('error')
