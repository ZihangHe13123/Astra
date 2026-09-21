import { test } from 'node:test';
import assert from 'node:assert/strict';
import { checkedCommand, checkedPreferences, externalURL, inside } from '../src/main/validation.js';

test('renderer cannot inject an Appshot manifest or send arbitrary backend control', () => {
  assert.throws(() => checkedCommand({ type:'message', text:'hello', submission_id:'abc', appshots:[{manifest_path:'/tmp/forged'}] }));
  assert.throws(() => checkedCommand({ type:'exit' }));
  assert.throws(() => checkedCommand({ type:'tool_approval_response', request_id:'x', decision:'always' }));
  assert.throws(() => checkedCommand({ type:'message', text:'hello', submission_id:'' }));
  assert.equal(checkedCommand({ type:'command', cmd:'/permissions' }).cmd, '/permissions');
  assert.equal(checkedCommand({ type:'refresh_models', provider_id:'codex', force:true }).provider_id, 'codex');
});
test('preference corruption is rejected before it can break rendering', () => {
  for (const data of [{projects:null},{drafts:[]},{timeline:'yes'},{pinned:[1]},{__extra:'x'},{detailWidth:Infinity},{detailWidth:100},{detailWidth:'420'}]) assert.throws(()=>checkedPreferences(data));
  assert.deepEqual(checkedPreferences({drafts:{'local:s':'你好'},projects:['/tmp/工作区'],timeline:true}),
    {drafts:{'local:s':'你好'},projects:['/tmp/工作区'],timeline:true});
});
test('external links and artifact directory boundaries are explicit', () => {
  for (const url of ['file:///etc/passwd','javascript:alert(1)','https://user:password@example.org/']) assert.throws(()=>externalURL(url));
  assert.equal(externalURL('https://example.org/a?q=1'), 'https://example.org/a?q=1');
  assert.equal(inside('/project', '/project/a.md'), true);
  assert.equal(inside('/project', '/project/..notes'), true);
  assert.equal(inside('/project', '/project-other/a.md'), false);
  assert.equal(inside('/project', '/etc/passwd'), false);
});

test('model selection accepts only a single model and blank pages clear restored sessions',()=>{
 assert.throws(()=>checkedCommand({type:'select_model',model_key:'one\n/yolo on',request_id:'x'}));
 assert.equal(checkedCommand({type:'select_model',model_key:'codex::model',request_id:'x'}).model_key,'codex::model');
 assert.deepEqual(checkedPreferences({lastSession:null}),{lastSession:null});
});
