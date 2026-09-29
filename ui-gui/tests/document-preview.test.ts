import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile, readFile, mkdir, symlink, unlink } from 'node:fs/promises';
import { join, sep } from 'node:path';
import { realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import * as previews from '../src/main/file-preview.js';

const pdf = Buffer.from('%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n');
async function fixture(run: (dir: string) => Promise<void>) { const dir = await mkdtemp(join(tmpdir(), 'astra-doc-preview-')); try { await run(dir); } finally { await rm(dir, {recursive:true, force:true}); } }

test('PDF preview returns complete PDF bytes and source identity rather than text', () => fixture(async dir => {
  const path = join(dir, 'document.pdf'); await writeFile(path, pdf);
  const result = await previews.filePreview(path);
  assert.equal(result.kind, 'pdf'); assert.ok(result.sourceVersion);
  assert.equal(result.data, `data:application/pdf;base64,${pdf.toString('base64')}`);
}));

test('PDF preview rejects invalid PDF and oversized files', () => fixture(async dir => {
  const path = join(dir, 'document.pdf'); await writeFile(path, 'not a pdf');
  await assert.rejects(previews.filePreview(path), /PDF/);
  const service = new previews.DocumentPreviewService({maxInputBytes:32});
  await writeFile(path, pdf); await assert.rejects(service.preview(path), /大小限制/);
}));

test('Office conversion caches by source version, rechecks source, and keeps originals intact', () => fixture(async dir => {
  const path = join(dir, 'report.docx'); await writeFile(path, 'office fixture'); let conversions = 0;
  const service = new previews.DocumentPreviewService({inspectOffice: () => ({format:'docx', status:'passed', parts:1, warnings:[]}), convert: async () => { conversions++; return {pdf, missingFonts:['Absent Font']}; }});
  const original = await readFile(path); const first = await service.preview(path); const cached = await service.preview(path);
  assert.equal(conversions, 1); assert.equal(first.kind, 'pdf'); assert.equal(cached.cached, true); assert.deepEqual(first.missingFonts, ['Absent Font']);
  assert.deepEqual(await readFile(path), original);
  await writeFile(path, 'updated office fixture'); const next = await service.preview(path);
  assert.equal(conversions, 2); assert.notEqual(first.sourceVersion, next.sourceVersion);
}));

test('Office conversion refuses stale output when the source changes during rendering', () => fixture(async dir => {
  const path = join(dir, 'report.pptx'); await writeFile(path, 'office fixture');
  const service = new previews.DocumentPreviewService({inspectOffice: () => ({format:'pptx', status:'passed', parts:1, warnings:[]}), convert: async () => { await writeFile(path, 'changed during render'); return {pdf, missingFonts:[]}; }});
  await assert.rejects(service.preview(path), /源文件已变化/);
}));

test('Office queue is bounded and cancelled queued work never reaches converter', () => fixture(async dir => {
  const paths = ['first.docx','second.docx','third.docx'].map(name=>join(dir,name));
  await Promise.all(paths.map(path=>writeFile(path,'office fixture')));
  let release!: () => void; const gate = new Promise<void>(resolve=>{release=resolve}); let started!: () => void; const begin = new Promise<void>(resolve=>{started=resolve}); let conversions = 0;
  const service = new previews.DocumentPreviewService({maxQueued:1, inspectOffice: () => ({format:'docx', status:'passed', parts:1, warnings:[]}), convert: async () => {conversions++; started(); await gate; return {pdf, missingFonts:[]};}});
  const first = service.preview(paths[0]); await begin;
  const abort = new AbortController(); const second = service.preview(paths[1], abort.signal);
  await new Promise(resolve=>setImmediate(resolve));
  await assert.rejects(service.preview(paths[2]), /队列已满/);
  const cancelled = assert.rejects(second, /取消/); abort.abort(); await cancelled;
  release(); await first; assert.equal(conversions,1);
}));

test('active conversion receives cancellation and releases the next queued request', () => fixture(async dir => {
  const firstPath=join(dir,'cancel.docx'), nextPath=join(dir,'next.docx');await writeFile(firstPath,'first');await writeFile(nextPath,'next');
  let started!:()=>void;const begin=new Promise<void>(resolve=>{started=resolve;});let calls=0;
  const service=new previews.DocumentPreviewService({inspectOffice:()=>({format:'docx',status:'passed',parts:1,warnings:[]}),convert:async(_bytes,_format,signal)=>{
    calls++;if(calls===1){started();await new Promise((_,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true}));}return {pdf,missingFonts:[]};
  }});
  const controller=new AbortController();const first=service.preview(firstPath,controller.signal);await begin;
  const next=service.preview(nextPath);const rejected=assert.rejects(first,/取消/);controller.abort();await rejected;assert.equal((await next).kind,'pdf');assert.equal(calls,2);await service.dispose();
}));

test('invalid converter output is never cached or presented as a successful PDF', () => fixture(async dir => {
  const path=join(dir,'failed.docx');await writeFile(path,'office fixture');let calls=0;
  const service=new previews.DocumentPreviewService({inspectOffice:()=>({format:'docx',status:'passed',parts:1,warnings:[]}),convert:async()=>{calls++;return {pdf:Buffer.from('invalid'),missingFonts:[]};}});
  await assert.rejects(service.preview(path),/PDF/);await assert.rejects(service.preview(path),/PDF/);assert.equal(calls,2);
}));


test('queued Office reads revalidate session authorization after a symlink replacement', () => fixture(async dir => {
  const inside=join(dir,'workspace');await mkdir(inside);
  const firstPath=join(inside,'first.docx'), selected=join(inside,'selected.docx'), outside=join(dir,'private.docx');
  await Promise.all([firstPath,selected,outside].map(path=>writeFile(path,'office fixture')));
  const authorized=realpathSync(selected), root=realpathSync(inside);let release!:()=>void, started!:()=>void;
  const gate=new Promise<void>(resolve=>{release=resolve;}), begin=new Promise<void>(resolve=>{started=resolve;});
  const service=new previews.DocumentPreviewService({inspectOffice:()=>({format:'docx',status:'passed',parts:1,warnings:[]}),convert:async()=>{started();await gate;return {pdf,missingFonts:[]};}});
  const first=service.preview(firstPath);await begin;
  const second=service.preview(authorized,undefined,()=>{const current=realpathSync(selected);if(!current.startsWith(root+sep))throw new Error('Outside authorized workspace');return current;});
  await unlink(selected);await symlink(outside,selected);
  const rejected=assert.rejects(second,/authorized/);release();await first;await rejected;
}));

test('authorization revoked while the descriptor opens rejects before Office parsing or conversion', () => fixture(async dir => {
  const path=join(dir,'source.docx');await writeFile(path,'fixture');const canonical=realpathSync(path);let checks=0, parsed=0;
  const service=new previews.DocumentPreviewService({inspectOffice:()=>{parsed++;return {format:'docx',status:'passed',parts:1,warnings:[]};},convert:async()=>({pdf,missingFonts:[]})});
  await assert.rejects(service.preview(canonical,undefined,()=>{if(++checks>=2)throw new Error('Session is not open');return canonical;}),/Session is not open/);
  assert.equal(parsed,0);
}));

test('a closed session cannot consume queued Office source bytes', () => fixture(async dir => {
  const one=join(dir,'first.docx'),two=join(dir,'queued.docx');await writeFile(one,'first');await writeFile(two,'queued');const path=realpathSync(two);
  let release!:()=>void,started!:()=>void;const gate=new Promise<void>(resolve=>{release=resolve;}),begin=new Promise<void>(resolve=>{started=resolve;});let open=true,calls=0;
  const service=new previews.DocumentPreviewService({inspectOffice:()=>({format:'docx',status:'passed',parts:1,warnings:[]}),convert:async()=>{calls++;started();await gate;return {pdf,missingFonts:[]};}});
  const first=service.preview(one);await begin;const queued=service.preview(path,undefined,()=>{if(!open)throw new Error('Session is not open');return path;});open=false;
  const denied=assert.rejects(queued,/Session is not open/);release();await first;await denied;assert.equal(calls,1);
}));
