import {test} from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { DocumentPreviewService } from '../src/main/file-preview.js';

for(const format of ['docx','pptx','xlsx']) test(`native saved ${format} preview, local conversion and preserved source`,{skip:process.env.ASTRA_TEST_NATIVE_OFFICE!=='1',timeout:90000},async()=>{
 const path=fileURLToPath(new URL(`./fixtures/documents/preview.${format}`,import.meta.url));const before=await readFile(path);
 const service=new DocumentPreviewService();
 try {
  const result=await service.preview(path);
  assert.equal(result.checks?.status,'passed');
  assert.equal(result.kind,format==='xlsx'?'spreadsheet':'pdf');
  if(format==='xlsx') {assert.equal(result.sheets?.[0].rows[1][0],'中文');assert.equal(result.sheets?.[0].rows[1][2],'（公式未缓存）');}
  else {assert.ok(result.data?.startsWith('data:application/pdf;base64,'));assert.ok(result.converted);}
  if(format==='docx')assert.ok(result.missingFonts?.includes('Astra Fixture Missing Font'));
  assert.deepEqual(await readFile(path),before);
 } finally {await service.dispose();}
});
