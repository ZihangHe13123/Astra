import test from 'node:test';
import assert from 'node:assert/strict';
import {installWorker} from '../worker.mjs';
const event=()=>({listeners:[],addListener(fn){this.listeners.push(fn);},fire(...args){for(const f of this.listeners)f(...args);}});
const tick=()=>new Promise(r=>setImmediate(r));
function fixture(local={},session={}){
 const ports=[],alarms=new Map();
 const area=data=>({get:async()=>structuredClone(data),set:async values=>Object.assign(data,structuredClone(values))});
 const api={storage:{local:area(local),session:area(session)},alarms:{onAlarm:event(),create:(name,value)=>alarms.set(name,value),clear:async name=>alarms.delete(name)},runtime:{id:'self',getURL:x=>'chrome-extension://self/'+x,onMessage:event(),onStartup:event(),connectNative(){const p={onMessage:event(),onDisconnect:event(),posted:[],postMessage(m){this.posted.push(m);},disconnect(){this.onDisconnect.fire();}};ports.push(p);return p;}},tabs:{onUpdated:event(),onRemoved:event(),get:async id=>({id,url:'https://example.com',title:'Example'})},permissions:{contains:async()=>true,onRemoved:event()}};
 const worker=installWorker(api,{retryDelays:[]});
 const ui=message=>new Promise(resolve=>api.runtime.onMessage.listeners[0](message,{id:'self',url:'chrome-extension://self/popup.html'},resolve));
 const ready=async()=>{ports.at(-1).onMessage.fire({type:'astra_control_ready',version:1});await tick();await tick();};
 const connect=async()=>{await ui({operation:'connect'});await ready();};
 return {api,worker,ports,ui,ready,connect,local,session,alarms};
}
test('manual mode waits for authenticated ready and disconnect clears grants',async()=>{const f=fixture();await tick();assert.equal(f.ports.length,0);await f.ui({operation:'connect'});assert.equal(f.worker.control.state().enabled,false);await f.ready();await f.ui({operation:'grant',tabId:1});assert.deepEqual(f.worker.control.state().grantedTabIds,[1]);f.ports[0].onDisconnect.fire();await tick();assert.equal(f.worker.control.state().enabled,false);assert.deepEqual(f.worker.control.state().grantedTabIds,[]);assert.equal(f.ports.length,1);});
test('native error is visible while retrying and clears after successful handshake',async()=>{
 const f=fixture({autoConnect:true});await tick();
 f.api.runtime.lastError={message:'Specified native messaging host not found.'};f.ports[0].onDisconnect.fire();delete f.api.runtime.lastError;
 let state=(await f.ui({operation:'state'})).result;
 assert.equal(state.connected,false);assert.equal(state.connectionState,'waiting');assert.match(state.connectionError,/host not found/);
 await f.connect();assert.equal((await f.ui({operation:'state'})).result.connectionError,'');
 f.ports[0].onDisconnect.fire();assert.equal((await f.ui({operation:'state'})).result.connectionError,'');
 await f.ui({operation:'stop'});
});
test('synchronous native errors are bounded plain text and stop clears them',async()=>{
 const f=fixture();await tick();f.api.runtime.connectNative=()=>{throw Error('blocked\n'+ 'x'.repeat(500));};
 const state=(await f.ui({operation:'connect'})).result;
 assert.equal(state.connected,false);assert.equal(state.connectionError.length,400);assert.ok(!state.connectionError.includes('\n'));
 assert.equal((await f.ui({operation:'stop'})).result.connectionError,'');
});
test('auto startup waits for ready and restores exact owned paused session grants',async()=>{const f=fixture({autoConnect:true},{grants:[{tabId:1,origin:'https://example.com',owned:true,paused:true}]});await tick();assert.equal(f.ports.length,1);assert.equal(f.worker.control.state().enabled,false);await f.ready();assert.deepEqual(f.worker.control.exportGrants(),[{tabId:1,origin:'https://example.com',owned:true,paused:true}]);assert.equal((await f.ui({operation:'state'})).result.connected,true);});
test('stop while connecting persists opt-out and cancels alarm across worker wake',async()=>{const f=fixture({autoConnect:true});await tick();await f.ui({operation:'stop'});await f.ready();assert.equal(f.worker.control.state().enabled,false);assert.equal(f.local.autoConnect,false);assert.deepEqual(f.session.grants,[]);assert.equal(f.alarms.size,0);const g=fixture(f.local,f.session);await tick();assert.equal(g.ports.length,0);});
test('offline navigation and removal revoke saved grants before reconnect',async()=>{const f=fixture({autoConnect:true},{grants:[{tabId:1,origin:'https://example.com',owned:false,paused:false}]});await tick();await f.ready();f.ports[0].onDisconnect.fire();f.api.tabs.onUpdated.fire(1,{url:'https://other.test'});await tick();await f.ui({operation:'connect'});await f.ready();assert.deepEqual(f.worker.control.state().grantedTabIds,[]);assert.deepEqual(f.session.grants,[]);});
test('offline permission removal revokes persisted grants',async()=>{const f=fixture({autoConnect:true},{grants:[{tabId:1,origin:'https://example.com',owned:false,paused:false}]});await tick();await f.ready();f.ports[0].onDisconnect.fire();f.api.permissions.contains=async()=>false;f.api.permissions.onRemoved.fire({origins:['https://example.com/*']});await tick();await tick();assert.deepEqual(f.session.grants,[]);});
test('requests behind ready restoration never replay into another port',async()=>{const f=fixture({autoConnect:true});await tick();let resolve;f.api.tabs.get=()=>new Promise(r=>resolve=r);f.session.grants=[{tabId:1,origin:'https://example.com',owned:false,paused:false}]; // no grants loaded: dispatch gate still runs asynchronously
 f.ports[0].onMessage.fire({type:'astra_control_ready',version:1});f.ports[0].onMessage.fire({id:'old',operation:'tabs'});f.ports[0].onDisconnect.fire();await tick();await f.ui({operation:'connect'});await f.ready();assert.deepEqual(f.ports.map(p=>p.posted.filter(m=>!['astra_control_ready','astra_control_capabilities'].includes(m.id))),[[],[]]);});
test('content scripts cannot enable control',async()=>{const f=fixture();const response=await new Promise(r=>f.api.runtime.onMessage.listeners[0]({operation:'connect'},{id:'self',url:'https://example.com',tab:{id:1}},r));assert.equal(response.ok,false);});
test('stop during initial storage read never opens a native port',async()=>{
 let release;const f=fixture({autoConnect:true});
 // Initialization enters local.get on its first microtask.
 f.api.storage.local.get=()=>new Promise(r=>release=r);
 await tick();const stopped=f.ui({operation:'stop'});release({autoConnect:true});await stopped;
 assert.equal(f.ports.length,0);assert.equal(f.local.autoConnect,false);
});
test('ready waits for asynchronous restore and rejects removed tab without resurrection',async()=>{
 const f=fixture({autoConnect:true},{grants:[{tabId:1,origin:'https://example.com',owned:true,paused:true}]});await tick();
 let release;f.api.tabs.get=()=>new Promise(r=>release=r);
 f.ports[0].onMessage.fire({type:'astra_control_ready',version:1});f.ports[0].onMessage.fire({id:'queued',operation:'tabs'});await tick();
 assert.equal(f.worker.control.state().enabled,false);assert.deepEqual(f.ports[0].posted,[]);
 f.api.tabs.onRemoved.fire(1);release({id:1,url:'https://example.com'});await tick();await tick();
 assert.deepEqual(f.session.grants,[]);assert.deepEqual(f.ports[0].posted.find(m=>m.id==='queued').result.tabs,[]);
});
test('worker restart retains session grant; browser session reset retains only opt-in',async()=>{
 const local={autoConnect:true},session={grants:[{tabId:1,origin:'https://example.com',owned:false,paused:true}]};
 const f=fixture(local,session);await tick();await f.ready();const g=fixture(local,session);await tick();await g.ready();assert.deepEqual(g.worker.control.state().grantedTabIds,[1]);
 const h=fixture(local,{});await tick();await h.ready();assert.deepEqual(h.worker.control.state().grantedTabIds,[]);
});
test('restore validates exact origin private missing tab and permission',async()=>{
 const grants=[1,2,3,4,5].map(tabId=>({tabId,origin:'https://example.com',owned:false,paused:false}));const f=fixture({autoConnect:true},{grants});
 f.api.tabs.get=async id=>{if(id===4)throw Error('gone');return {id,url:id===2?'https://other.test':'https://example.com',incognito:id===3};};f.api.permissions.contains=async()=>false;
 await tick();await f.ready();assert.deepEqual(f.worker.control.state().grantedTabIds,[]);
});
test('alarm reconnect is one attempt per live port and stop clears it',async()=>{const f=fixture({autoConnect:true});await tick();f.ports[0].onDisconnect.fire();await tick();assert.equal(f.alarms.size,1);f.api.alarms.onAlarm.fire({name:'astra-browser-reconnect'});await tick();f.api.alarms.onAlarm.fire({name:'astra-browser-reconnect'});await tick();assert.equal(f.ports.length,2);await f.ui({operation:'stop'});assert.equal(f.alarms.size,0);});
test('stop wins over an in-flight grant storage write',async()=>{
 const f=fixture({autoConnect:true});await tick();await f.ready();
 let release;const original=f.api.storage.session.set;let first=true;
 f.api.storage.session.set=async value=>{if(first){first=false;await new Promise(r=>release=r);}await original(value);};
 const granting=f.ui({operation:'grant',tabId:1});await tick();const stopping=f.ui({operation:'stop'});release();await granting;await stopping;
 assert.deepEqual(f.session.grants,[]);assert.equal(f.local.autoConnect,false);assert.equal(f.worker.control.state().enabled,false);
});
test('state is not connected during permission revalidation',async()=>{
 const f=fixture({autoConnect:true});await tick();await f.ready();await f.ui({operation:'grant',tabId:1});
 let release;f.api.tabs.get=()=>new Promise(r=>release=r);f.api.permissions.onRemoved.fire({origins:['https://other.test/*']});await tick();
 assert.equal((await f.ui({operation:'state'})).result.connected,false);
 release({id:1,url:'https://example.com'});await tick();
});
test('a submitted old-connection request cannot emit into reconnect',async()=>{
 const f=fixture({autoConnect:true});await tick();await f.ready();let finish,calls=0;
 f.worker.control.handle=()=>{calls++;return new Promise(r=>finish=r);};
 f.ports[0].onMessage.fire({id:'pending',operation:'click'});await tick();
 f.ports[0].onDisconnect.fire();await f.ui({operation:'connect'});await f.ready();
 finish({id:'pending',ok:true,result:{status:'acted'}});await tick();
 assert.equal(calls,1);assert.deepEqual(f.ports.map(p=>p.posted.filter(m=>!['astra_control_ready','astra_control_capabilities'].includes(m.id))),[[],[]]);
});
test('browser startup listener triggers auto mode without creating duplicate ports',async()=>{const f=fixture({autoConnect:true});assert.equal(f.api.runtime.onStartup.listeners.length,1);await tick();f.api.runtime.onStartup.fire();await tick();assert.equal(f.ports.length,1);});
test('queued permission restore cannot resurrect live grants after Stop',async()=>{const f=fixture({autoConnect:true});await tick();await f.ready();await f.ui({operation:'grant',tabId:1});f.api.permissions.onRemoved.fire({origins:['https://other.test/*']});await f.ui({operation:'stop'});assert.deepEqual(f.worker.control.exportGrants(),[]);assert.deepEqual(f.session.grants,[]);});
test('navigation during permission revalidation cannot retain revoked permission',async()=>{const f=fixture({autoConnect:true});await tick();await f.ready();await f.ui({operation:'grant',tabId:1});let release;f.api.tabs.get=()=>new Promise(r=>release=r);f.api.permissions.contains=async()=>false;f.api.permissions.onRemoved.fire({origins:['https://example.com/*']});await tick();f.api.tabs.onUpdated.fire(999,{url:'https://other.test'});release({id:1,url:'https://example.com'});await tick();await tick();assert.deepEqual(f.worker.control.exportGrants(),[]);assert.deepEqual(f.session.grants,[]);});
test('ready ACK is emitted once only after restoration and persistence',async()=>{const f=fixture({autoConnect:true},{grants:[{tabId:1,origin:'https://example.com',owned:true,paused:true}]});await tick();let release;f.api.tabs.get=()=>new Promise(r=>release=r);f.ports[0].onMessage.fire({type:'astra_control_ready',version:1});await tick();assert.deepEqual(f.ports[0].posted,[]);release({id:1,url:'https://example.com'});await tick();await tick();assert.deepEqual(f.ports[0].posted.map(m=>m.id),['astra_control_capabilities','astra_control_ready']);assert.deepEqual(f.ports[0].posted[1],{id:'astra_control_ready',ok:true,result:{version:1}});assert.ok(f.ports[0].posted[0].result.operations.includes('check'));assert.ok(!f.ports[0].posted[0].result.operations.includes('screenshot'));f.ports[0].onMessage.fire({type:'astra_control_ready',version:1});await tick();assert.equal(f.ports[0].posted.length,2);});
test('permission removal during ready persistence prevents premature ACK',async()=>{const f=fixture({autoConnect:true},{grants:[{tabId:1,origin:'https://example.com',owned:false,paused:false}]});await tick();let release;const original=f.api.storage.session.set;let first=true;f.api.storage.session.set=async values=>{if(first){first=false;await new Promise(r=>release=r);}await original(values);};f.ports[0].onMessage.fire({type:'astra_control_ready',version:1});await tick();f.api.permissions.contains=async()=>false;f.api.permissions.onRemoved.fire({origins:['https://example.com/*']});release();await tick();await tick();assert.deepEqual(f.ports[0].posted,[]);});
test('navigation of a later restore candidate cannot resurrect it after returning to original origin',async()=>{const f=fixture({autoConnect:true},{grants:[1,2].map(tabId=>({tabId,origin:'https://example.com',owned:false,paused:false}))});await tick();let release;f.api.tabs.get=async id=>id===1?await new Promise(r=>release=r):{id,url:'https://example.com'};f.ports[0].onMessage.fire({type:'astra_control_ready',version:1});await tick();f.api.tabs.onUpdated.fire(2,{url:'https://other.test'});f.api.tabs.onUpdated.fire(2,{url:'https://example.com'});release({id:1,url:'https://example.com'});await tick();await tick();assert.deepEqual(f.worker.control.state().grantedTabIds,[1]);});
test('an older permission handler cannot enable while newer revocation waits behind storage',async()=>{
 const f=fixture({autoConnect:true});await tick();await f.ready();await f.ui({operation:'grant',tabId:1});
 let releaseSession,releaseLocal;const setSession=f.api.storage.session.set,setLocal=f.api.storage.local.set;let first=true;
 f.api.storage.session.set=async value=>{if(first){first=false;await new Promise(r=>releaseSession=r);}await setSession(value);};
 f.api.permissions.onRemoved.fire({origins:['https://other.test/*']});await tick();
 f.api.storage.local.set=async value=>{await new Promise(r=>releaseLocal=r);await setLocal(value);};
 const toggle=f.ui({operation:'autoConnect',enabled:true});await tick();
 f.api.permissions.contains=async()=>false;f.api.permissions.onRemoved.fire({origins:['https://example.com/*']});releaseSession();await tick();
 assert.equal(f.worker.control.state().enabled,false);
 releaseLocal();await toggle;await tick();assert.deepEqual(f.worker.control.exportGrants(),[]);
});
