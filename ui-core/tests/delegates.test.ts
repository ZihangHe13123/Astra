import test from 'node:test';
import assert from 'node:assert/strict';
import { initialSession, projectEvent } from '../src/session-state.js';
import { reduceDelegateEvent, orderedDelegates } from '../src/delegates.js';

test('parallel children keep their own progress and survive parent completion', () => {
 let s = projectEvent(initialSession('a'), {type:'session_info',name:'session-a'});
 for (const id of ['one','two']) s=projectEvent(s,{type:'delegate_status',process_id:id,session_id:'session-a',goal:id,status:'running',updated_at:1});
 s=projectEvent(s,{type:'delegate_status',process_id:'one',status:'running',current_tool:'search_web',updated_at:2});
 s=projectEvent(s,{type:'delegate_status',process_id:'two',status:'queued',updated_at:2});
 s=projectEvent(s,{type:'task_status',task:{status:'completed'}});
 assert.equal(s.delegates.one.current_tool,'search_web');assert.equal(s.delegates.two.current_tool,undefined);
 assert.equal(s.delegates.one.status,'running');assert.equal(s.delegates.two.status,'queued');assert.equal(s.busy,false);
 s=projectEvent(s,{type:'delegate_status',process_id:'other',session_id:'another-session',status:'running'});
 assert.equal(s.delegates.other,undefined);
});

test('late progress cannot resurrect completion; idle children can awaken', () => {
 let d=reduceDelegateEvent({}, {process_id:'one',status:'idle',result:'old episode',result_truncated:true,updated_at:2});
 d=reduceDelegateEvent(d,{process_id:'one',status:'running',updated_at:3});assert.equal(d.one.status,'running');
 assert.equal(d.one.result,undefined);assert.equal(d.one.result_truncated,undefined);
 d=reduceDelegateEvent(d,{process_id:'one',status:'completed',result:'report',updated_at:4});
 d=reduceDelegateEvent(d,{process_id:'one',status:'running',updated_at:5});assert.equal(d.one.status,'completed');
 d=reduceDelegateEvent(d,{process_id:'one',status:'failed',updated_at:1});assert.equal(d.one.result,'report');
});

test('history restores independent terminal records and ends historical failures', () => {
 let s=projectEvent(initialSession('a'),{type:'delegate_status',process_id:'old',status:'running'});
 s=projectEvent(s,{type:'history',session_id:'b',messages:[],tool_results:[{name:'read_file',error:'denied',output:''},{name:'noop',output:''}],delegates:[{process_id:'saved',goal:'research',status:'completed',result:'report'}]});
 assert.deepEqual(Object.keys(s.delegates),['saved']);
 assert.deepEqual(s.tools.map(t=>[t.status,t.historical]),[['failed',true],['completed',true]]);
 s=projectEvent(s,{type:'history',messages:[],session_id:'c'});assert.deepEqual(s.delegates,{});
});

test('disconnect ends visible running markers without fabricating a result', () => {
 let s=initialSession('a');
 for (const [id,status] of [['one','running'],['two','completed']]) s=projectEvent(s,{type:'delegate_status',process_id:id,status,result:status==='completed'?'report':undefined,updated_at:1});
 s=projectEvent(s,{type:'tool_calls',calls:[{id:'parent',name:'delegate_task'}]});
 s=projectEvent(s,{type:'gui_disconnected'});
 assert.equal(s.delegates.one.status,'interrupted');assert.equal(s.delegates.one.result,undefined);
 assert.equal(s.delegates.two.status,'completed');assert.equal(s.tools[0].status,'interrupted');
 s=projectEvent(s,{type:'delegate_status',process_id:'one',status:'completed',result:'late report',updated_at:2});assert.equal(s.delegates.one.result,'late report');
});

test('large histories retain active work and only the newest 200 finished children', () => {
 let d=reduceDelegateEvent({}, {process_id:'running',status:'running',updated_at:0});
 for(let i=0;i<220;i++) d=reduceDelegateEvent(d,{process_id:String(i),status:'completed',updated_at:i+1});
 assert.equal(Object.keys(d).length,201);assert.equal(orderedDelegates(d)[0].process_id,'running');
 assert.equal(d['0'],undefined);assert.ok(d['219']);
});
