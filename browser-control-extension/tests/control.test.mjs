import test from 'node:test';
import assert from 'node:assert/strict';
import {createControl} from '../control.mjs';
function fixture() {
 const tabs = new Map([[1,{id:1,url:'https://example.com/a',title:'A',active:true,windowId:9}],[2,{id:2,url:'https://other.test',title:'B',active:false,windowId:9}]]);
 const removed=[], scripts=[];
 const api={tabs:{get:async id=>({...tabs.get(id)}),create:async ({url})=>{const t={id:3,url,title:'New',windowId:9,status:'complete'};tabs.set(3,t);return t;},remove:async id=>{removed.push(id);tabs.delete(id);},query:async()=>[...tabs.values()].filter(t=>t.active),captureVisibleTab:async()=> 'data:image/png;base64,AA'},windows:{get:async()=>({focused:true})},permissions:{contains:async()=>true},scripting:{executeScript:async o=>{scripts.push(o);return [{result:{status:'observed',url:tabs.get(o.target.tabId).url,elements:[]}}];}}};
 const c=createControl(api);let seq=0;
 const req=(operation,tabId,args={})=>c.handle({id:String(++seq),operation,tabId,args:{expectedOrigin:tabs.get(tabId)?.url ? new URL(tabs.get(tabId).url).origin : undefined,...args}});
 return {c,req,tabs,removed,scripts,api};
}
test('explicit enable and grant required; tabs disclose only granted tabs',async()=>{const f=fixture();assert.equal((await f.req('attach',1)).ok,false);f.c.enable();assert.equal((await f.req('attach',1)).ok,false);await f.c.grant(1);assert.equal((await f.req('attach',1)).ok,true);assert.deepEqual((await f.req('tabs')).result.tabs.map(t=>t.id),[1]);});
test('multiple grants need explicit attach target',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);await f.c.grant(2);assert.equal((await f.req('attach')).ok,false);});
test('stop revokes all access',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);f.c.stop();f.c.enable();assert.equal((await f.req('snapshot',1)).ok,false);});
test('cross origin rejects before injection and requires new grant',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);f.tabs.get(1).url='https://evil.test';assert.equal((await f.req('click',1)).ok,false);assert.equal(f.scripts.length,0);f.tabs.get(1).url='https://example.com/a';assert.equal((await f.req('snapshot',1)).ok,false);});
test('private/internal grants rejected',async()=>{const f=fixture();f.c.enable();f.tabs.get(1).incognito=true;await assert.rejects(f.c.grant(1));f.tabs.get(2).url='chrome://settings';await assert.rejects(f.c.grant(2));});
test('close detaches real user tab and closes owned tab',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);assert.equal((await f.req('close',1)).ok,true);assert.deepEqual(f.removed,[]);const opened=await f.req('open',undefined,{url:'https://example.com'});assert.equal(opened.result.tabId,3);await f.req('close',3);assert.deepEqual(f.removed,[3]);});
test('duplicate request IDs never dispatch twice',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);const r={id:'write',operation:'click',tabId:1,args:{selector:'#go',expectedOrigin:'https://example.com'}};const result=await Promise.all([f.c.handle(r),f.c.handle(r)]);assert.equal(result.filter(x=>x.ok).length,1);assert.equal(f.scripts.filter(x=>x.func).length,1);});
test('handoff blocks actions until explicit resume',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);await f.req('handoff',1);assert.equal((await f.req('click',1)).ok,false);assert.equal((await f.req('resume',1)).ok,true);assert.equal((await f.req('click',1)).ok,true);});
test('screenshot fails closed even for active granted tab',async()=>{const f=fixture();f.c.enable();await f.c.grant(2);assert.equal((await f.req('screenshot',2)).ok,false);await f.c.grant(1);assert.equal((await f.req('screenshot',1)).ok,false);});
test('unknown operations and overlarge request refused',async()=>{const f=fixture();f.c.enable();assert.equal((await f.req('eval',1)).ok,false);assert.equal((await f.req('type',1,{text:'x'.repeat(1048576)})).ok,false);});

test('revocation during injection prevents write dispatch',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);f.scripts.push=()=>{f.c.stop();return 1;};assert.equal((await f.req('click',1)).ok,false);});
test('metadata-only snapshot checks origin without invalidating refs',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);const r=await f.req('snapshot',1,{metadataOnly:true});assert.deepEqual(r.result,{url:'https://example.com/a',title:'A'});assert.equal(f.scripts.length,0);});

test('compact snapshot options and choice state survive isolated extension dispatch',async()=>{
 const f=fixture();f.c.enable();await f.c.grant(1);
 const snapshot={url:'https://example.com/a',text:'',textIncluded:false,elements:[{ref:'s:0',checked:false,indeterminate:true}]};
 f.api.scripting.executeScript=async o=>{
  f.scripts.push(o);
  if(o.files)return [];
  assert.equal(o.world,'ISOLATED');assert.equal(o.target.tabId,1);
  assert.equal(o.args[0],'snapshot');assert.equal(o.args[1].include_text,false);
  assert.equal(o.args[1].role_filter,'checkbox');assert.equal(o.args[1].expectedOrigin,'https://example.com');
  return [{result:snapshot}];
 };
 const r=await f.req('snapshot',1,{role_filter:'checkbox',include_text:false});
 assert.equal(r.ok,true,r.error);assert.deepEqual(r.result,snapshot);
 assert.equal(f.scripts.filter(o=>o.func).length,1);
});

test('approved origin survives popup regrant on another site',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);f.tabs.get(1).url='https://other.test';await f.c.grant(1);const r=await f.req('click',1,{selector:'#go',expectedOrigin:'https://example.com'});assert.equal(r.ok,false);assert.equal(f.scripts.length,0);});
test('grant replacement while injecting invalidates old request',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);f.api.scripting.executeScript=async o=>{f.scripts.push(o);if(o.files) await f.c.grant(1);return [{result:{status:'observed'}}];};const r=await f.req('click',1,{selector:'#go'});assert.equal(r.ok,false);assert.equal(f.scripts.filter(x=>x.func).length,0);});
for (const failure of ['reject','no-result','disconnect']) test('post-dispatch '+failure+' is unknown outcome',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);f.api.scripting.executeScript=async o=>{if(o.files)return [];if(failure==='reject')throw new Error('frame gone');if(failure==='disconnect')f.c.stop();return failure==='no-result'?[]:[{result:{status:'observed'}}];};const r=await f.req('click',1,{selector:'#go'});assert.equal(r.ok,true);assert.equal(r.result.status,'unknown_outcome');});
test('wait unmet conditions time out explicitly',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);f.api.scripting.executeScript=async o=>o.files?[]:[{result:o.args[0]==='probe'?{matched:false}:{url:'https://example.com/a',text:''}}];const r=await f.req('wait',1,{text:'missing',timeoutMs:0});assert.equal(r.result.status,'timeout');});
test('wait polls all conditions without taking snapshots between probes',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);let probes=0,snapshots=0;f.api.scripting.executeScript=async o=>{if(o.files)return [];if(o.args[0]==='probe'){assert.equal(o.args[1].ref,'s1:e1');assert.equal(o.args[1].text,'ready');assert.equal(o.args[1].urlContains,'example.com');return [{result:{matched:++probes===2}}];}snapshots++;return [{result:{url:'https://example.com/a'}}];};const r=await f.req('wait',1,{ref:'s1:e1',text:'ready',urlContains:'example.com',timeoutMs:1000});assert.equal(r.result.status,'observed');assert.equal(probes,2);assert.equal(snapshots,1);});

test('writes require an approved origin',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);const r=await f.c.handle({id:'missing-origin',operation:'click',tabId:1,args:{selector:'#go'}});assert.equal(r.ok,false);assert.equal(f.scripts.length,0);});

test('open waits for the committed document before the first snapshot',async()=>{
 const f=fixture();f.c.enable();let reads=0,creates=0;
 f.api.tabs.create=async()=>{creates++;const t={id:3,url:'',pendingUrl:'https://example.com/',status:'loading'};f.tabs.set(3,t);return t;};
 const get=f.api.tabs.get;
 f.api.tabs.get=async id=>{if(id===3 && ++reads===3)f.tabs.set(3,{id:3,url:'https://example.com/',status:'complete',title:'Loaded'});return get(id);};
 const opened=await f.req('open',undefined,{url:'https://example.com/'});
 assert.equal(opened.ok,true,opened.error);
 const snapshot=await f.req('snapshot',3);
 assert.equal(snapshot.ok,true,snapshot.error);
 assert.equal(opened.result.title,'Loaded');assert.equal(creates,1);assert.ok(reads>=3);
});
test('open rejects cross-origin redirects and removes only its new tab',async()=>{
 const f=fixture();f.c.enable();
 f.api.tabs.create=async()=>{const t={id:3,url:'https://other.test/',status:'complete'};f.tabs.set(3,t);return t;};
 const r=await f.req('open',undefined,{url:'https://example.com/'});
 assert.equal(r.ok,false);assert.match(r.error,/origin/i);assert.deepEqual(f.removed,[3]);assert.deepEqual(f.c.state().grantedTabIds,[]);
});
test('stop during new-tab loading prevents attachment and cleans up',async()=>{
 const f=fixture();f.c.enable();
 f.api.tabs.create=async()=>{const t={id:3,url:'',status:'loading'};f.tabs.set(3,t);return t;};
 const get=f.api.tabs.get;f.api.tabs.get=async id=>{if(id===3)f.c.stop();return get(id);};
 const r=await f.req('open',undefined,{url:'https://example.com/'});
 assert.equal(r.ok,false);assert.match(r.error,/stopped|disconnected/i);assert.deepEqual(f.removed,[3]);assert.deepEqual(f.c.state().grantedTabIds,[]);
});
test('open timeout leaves no granted or orphan agent tab',async()=>{
 const f=fixture();const c=createControl(f.api,{openTimeoutMs:0});c.enable();
 f.api.tabs.create=async()=>{const t={id:3,url:'',status:'loading'};f.tabs.set(3,t);return t;};
 const r=await c.handle({id:'timeout',operation:'open',args:{url:'https://example.com/'}});
 assert.equal(r.ok,false);assert.match(r.error,/timed out/i);assert.deepEqual(f.removed,[3]);assert.deepEqual(c.state().grantedTabIds,[]);
});
test('initial navigation of an agent-created tab does not cancel open',async()=>{const f=fixture();f.c.enable();const create=f.api.tabs.create;f.api.tabs.create=async options=>{const tab=await create(options);f.c.navigation(tab.id,tab.url);return tab;};assert.equal((await f.req('open',undefined,{url:'https://example.com'})).ok,true);assert.deepEqual(f.removed,[]);});
test('unrelated navigation does not cancel a pending granted-tab read',async()=>{const f=fixture();f.c.enable();await f.c.grant(1);const get=f.api.tabs.get;f.api.tabs.get=async id=>{f.c.navigation(999,'https://other.test');return get(id);};assert.equal((await f.req('attach',1)).ok,true);});

test('same-tab page commands serialize and queued writes retain grant generation',async()=>{
 const f=fixture();f.c.enable();await f.c.grant(1);
 let release,started;const ready=new Promise(r=>{started=r});const gate=new Promise(r=>{release=r});
 let dispatches=0;
 f.api.scripting.executeScript=async o=>{
   f.scripts.push(o);if(o.files)return [];
   dispatches++;started();await gate;return [{result:{status:'verified'}}];
 };
 const first=f.req('fill',1,{selector:'#a',text:'A'});await ready;
 const second=f.req('fill',1,{selector:'#b',text:'B'});
 await new Promise(r=>setTimeout(r,10));assert.equal(dispatches,1);
 f.c.navigation(1,'https://example.com/changed');release();
 assert.equal((await first).ok,true);
 const queued=await second;assert.equal(queued.ok,false);assert.match(queued.error,/grant changed/i);assert.equal(dispatches,1);
});

test('fill requires write approval; scoped snapshot and read stay background operations',async()=>{
 const f=fixture();f.c.enable();await f.c.grant(1);
 const denied=await f.c.handle({id:'fill-unapproved',operation:'fill',tabId:1,args:{selector:'#a',text:'A'}});
 assert.equal(denied.ok,false);assert.equal(f.scripts.length,0);
 assert.equal((await f.req('snapshot',1,{scope:'editable',frame_ref:'f1',offset:1,limit:4})).ok,true);
 assert.equal((await f.req('read',1,{ref:'s1:e1'})).ok,true);
 assert.ok(f.scripts.every(o=>o.world==='ISOLATED' && o.target.tabId===1));
});

for(const action of ['stop','disconnect','revoke','handoff','navigation']) test('pending file buffers invalidated on '+action,async()=>{
 const f=fixture();f.c.enable();await f.c.grant(1);
 assert.ok(f.c.capabilities().operations.includes('upload_prepare'));
 await f.req('upload_prepare',1,{ref:'file',files:[]});
 const before=f.scripts.length;
 if(action==='revoke')f.c.revoke(1);
 else if(action==='handoff')await f.req('handoff',1);
 else if(action==='navigation')f.c.navigation(1,'https://example.com/new');
 else f.c[action]();
 const cleanup=f.scripts.slice(before).filter(s=>s.func && s.args?.length===1);
 assert.equal(cleanup.length,1);
 assert.equal(cleanup[0].target.tabId,1);
 assert.equal(cleanup[0].world,'ISOLATED');
});

test('file transfer requires origin approval and never activates tabs',async()=>{
 const f=fixture();f.c.enable();await f.c.grant(1);
 for(const operation of ['upload_prepare','upload_chunk','upload_commit','upload_abort']) {
  const r=await f.c.handle({id:operation,operation,tabId:1,args:{}});
  assert.equal(r.ok,false);assert.equal(f.scripts.length,0);
 }
 await f.req('upload_prepare',1,{ref:'file',files:[]});
 assert.deepEqual(f.scripts[0].files,['file-upload.js','page.js']);
});

test('uncertain file commit is not retried',async()=>{
 const f=fixture();f.c.enable();await f.c.grant(1);
 let count=0;f.api.scripting.executeScript=async o=>{if(o.files)return [];count++;throw Error('lost response')};
 const result=await f.req('upload_commit',1,{transferId:'t'});
 assert.equal(result.result.status,'unknown_outcome');assert.equal(count,1);
});

test('open redirect to another origin names where the page went',async()=>{
 const f=fixture();f.c.enable();
 f.api.tabs.create=async()=>{const t={id:3,url:'https://www.example.com/a?x=1',status:'complete'};f.tabs.set(3,t);return t;};
 const r=await f.req('open',undefined,{url:'http://example.com/a?x=1'});
 assert.equal(r.ok,false);assert.ok(r.error.includes('https://www.example.com/a?x=1'),r.error);
 // The tab is gone, so the advice is to open the address, not to grant the tab.
 assert.deepEqual(f.removed,[3]);assert.match(r.error,/open that address/i);assert.doesNotMatch(r.error,/grant this tab/i);
 // An address too long for the 500-character error is reduced to its origin.
 f.api.tabs.create=async()=>{const t={id:4,url:'https://www.example.com/'+'p'.repeat(400),status:'complete'};f.tabs.set(4,t);return t;};
 const long=await f.req('open',undefined,{url:'http://example.com/'});
 assert.ok(long.error.includes('went to https://www.example.com and'),long.error);
});
test('open timeout says when the tab address never became readable',async()=>{
 const f=fixture();const c=createControl(f.api,{openTimeoutMs:0});c.enable();
 f.api.tabs.create=async()=>{const t={id:3,url:'',status:'loading'};f.tabs.set(3,t);return t;};
 const hidden=await c.handle({id:'hidden',operation:'open',args:{url:'https://example.com/'}});
 assert.match(hidden.error,/timed out/i);assert.match(hidden.error,/before the page address could be read/);
 f.api.tabs.create=async()=>{const t={id:4,url:'https://example.com/',status:'loading'};f.tabs.set(4,t);return t;};
 const slow=await c.handle({id:'slow',operation:'open',args:{url:'https://example.com/'}});
 assert.match(slow.error,/timed out/i);assert.doesNotMatch(slow.error,/before the page address could be read/);
 assert.deepEqual(f.removed,[3,4]);
});
for(const how of ['request','event']) test('an agent-opened tab that leaves its origin reports the new address ('+how+')',async()=>{
 const f=fixture();f.c.enable();
 assert.equal((await f.req('open',undefined,{url:'https://example.com/start'})).ok,true);
 f.tabs.get(3).url='https://other.test/next';
 if(how==='event') f.c.navigation(3,'https://other.test/next');
 const args={expectedOrigin:'https://example.com'};
 const first=await f.c.handle({id:'first',operation:'snapshot',tabId:3,args});
 assert.equal(first.ok,false);assert.ok(first.error.includes('https://other.test/next'),first.error);
 // Still no control of it, and no script was injected into the other site.
 assert.deepEqual(f.c.state().grantedTabIds,[]);assert.equal(f.scripts.length,0);
 const again=await f.c.handle({id:'again',operation:'click',tabId:3,args:{...args,selector:'#go'}});
 assert.equal(again.ok,false);assert.ok(again.error.includes('https://other.test/next'),again.error);
 assert.deepEqual((await f.req('tabs')).result.tabs,[]);
 // Closing the tab forgets it.
 f.c.revoke(3);
 assert.match((await f.c.handle({id:'closed',operation:'snapshot',tabId:3,args})).error,/explicit popup grant/);
});
test('a user-granted tab that leaves its origin is not described',async()=>{
 const f=fixture();f.c.enable();await f.c.grant(1);
 f.tabs.get(1).url='https://private.test/account';
 const args={expectedOrigin:'https://example.com'};
 for(const id of ['first','again']) {
  const r=await f.c.handle({id,operation:'snapshot',tabId:1,args});
  assert.equal(r.ok,false);assert.doesNotMatch(r.error,/private\.test/);
 }
 await f.c.grant(2);f.c.navigation(2,'https://private.test/other');
 const r=await f.c.handle({id:'event',operation:'snapshot',tabId:2,args:{expectedOrigin:'https://other.test'}});
 assert.equal(r.ok,false);assert.doesNotMatch(r.error,/private\.test/);
 assert.deepEqual(f.c.state().grantedTabIds,[]);
});
