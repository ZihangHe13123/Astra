import { test } from 'node:test';
import assert from 'node:assert/strict';
import { messageOffsets, windowRange } from '../src/renderer/message-window.js';
import { sessionTitle, sidebarGroups } from '../src/renderer/sidebar.js';
import { initialSession } from '@astra/ui-core/session-state';

test('large message windows remain bounded and cover scroll targets including both ends', () => {
 const messages=Array.from({length:50000},(_,i)=>({id:String(i),content:'x'.repeat(i%5*100)}));
 const offsets=messageOffsets(messages,new Map([['4',1200]]));
 assert.equal(offsets[5]-offsets[4],1200);
 for (const i of [0,4,15000,49999]) {
  const range=windowRange(offsets,offsets[i],800);
  assert.ok(range.start<=i && range.end>i); assert.ok(range.end-range.start<=100);
 }
 assert.deepEqual(windowRange([0],0,800),{start:0,end:0});
});
test('prepended pages preserve measured sizes by stable id', () => {
 const sizes=new Map([['old',900]]);
 const old=messageOffsets([{id:'old',content:'text'}],sizes);
 const next=messageOffsets([{id:'earlier',content:'other'},{id:'old',content:'text'}],sizes);
 assert.equal(next[2]-next[1],old[1]);
});
test('sidebar deduplicates pinned/live/history while retaining disconnected sessions and search', () => {
 const entries=[{name:'one',mode:'work',modified:2},{name:'two',mode:'work',modified:1}];
 const states=[{...initialSession('id','/tmp'),session:'one',isDraft:false,status:'ready' as const}, {...initialSession('other','/tmp'),session:'other',isDraft:false,status:'disconnected' as const}];
 const groups=sidebarGroups(entries,states,{titles:{'work:one':'First'},pinned:['work:one']},'');
 assert.equal(groups.pinned.length,1); assert.deepEqual(groups.open.map(x=>x.name),['other']);
 assert.deepEqual(groups.recent.flatMap(x=>x.entries.map(e=>e.name)),['two']);
 assert.equal(groups.all.length,3);
 assert.equal(sidebarGroups(entries,states,{titles:{'work:one':'First'},pinned:[]},'first').all.length,1);
 assert.equal(sessionTitle({name:'session_20260921051059_abcd',mode:'work',modified:0},{}),'09/21 05:10 的对话');
});
