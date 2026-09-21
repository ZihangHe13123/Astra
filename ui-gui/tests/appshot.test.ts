import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { DesktopAppshot } from '../src/main/appshot.js';

const offer = (id='capture') => ({ requestId:id, manifestPath:'/verified/'+id+'.json', appLabel:'Fixture', windowTitle:'Window',
  binding:{ instance_id:'broker', session_id:'recipient', recipient:{pid:1,process_start:'2',user_sid:'S-1-5-21'} } });
const commit = (id='capture') => ({type:'attach_commit',version:2,platform:'windows',request_id:id,manifest_path:'/verified/'+id+'.json',broker_id:'broker',session_id:'recipient'} as any);
function fixture() {
 let active='a'; const events:any[]=[]; const releases:string[]=[];
 const fake = Object.assign(new EventEmitter(), {state:{connection:'connected'},activityNS:1n,updateDraft(){},recordInput(){},release(id:string){releases.push(id);}});
 const manager = new DesktopAppshot(()=>active,()=>true,(id,event)=>events.push({id,event}),()=>fake as any);
 return {manager,events,releases,select:(id:string)=>active=id};
}
test('capture commit remains bound to the selected runtime and cannot switch recipients',()=>{
 const f=fixture(); assert.equal(f.manager.consumer.stageWindows!(offer()),true);
 f.select('b'); assert.throws(()=>f.manager.consumer.commit(commit()),/attachment_rejected/);
 assert.equal(f.manager.get('b').draft.attachments.length,0);
 f.select('a'); f.manager.consumer.commit(commit());
 assert.equal(f.manager.get('a').draft.attachments.length,1);
});
test('Appshot is acknowledged only after local incorporation; unknown submission cannot be resent',()=>{
 const f=fixture(); f.manager.consumer.stageWindows!(offer()); f.manager.consumer.commit(commit());
 const request=f.manager.prepare('a',{type:'message',text:'Describe',submission_id:'send1'});
 assert.equal(request.appshots.length,1); assert.equal(request.appshot_session_id,'recipient');
 f.manager.handle('a',{type:'submission_status',submission_id:'send1',status:'unknown'});
 assert.throws(()=>f.manager.prepare('a',{type:'message',text:'Again',submission_id:'send2'}));
 f.manager.handle('a',{type:'message_accepted',submission_id:'send1'});
 assert.deepEqual(f.releases,['capture']); assert.equal(f.manager.get('a').pending,undefined);
});
test('rejection restores attachments but broker disconnect revokes unsent authority',()=>{
 const f=fixture(); f.manager.consumer.stageWindows!(offer()); f.manager.consumer.commit(commit());
 f.manager.prepare('a',{type:'message',text:'Describe',submission_id:'send1'});
 f.manager.handle('a',{type:'message_rejected',submission_id:'send1'});
 assert.equal(f.manager.get('a').draft.attachments.length,1);
 f.manager.consumer.disconnect({reason:'recipient_disconnected',unsent:'revoked',pending:'unknown'});
 assert.equal(f.manager.get('a').draft.attachments.length,0);
});
test('closing a runtime releases unsent captures and keeps sent captures uncertain',()=>{
 const f=fixture(); f.manager.consumer.stageWindows!(offer()); f.manager.consumer.commit(commit());
 f.manager.handle('a',{type:'gui_disconnected'});
 assert.equal(f.manager.get('a').draft.attachments.length,0); assert.deepEqual(f.releases,['capture']);
 f.manager.consumer.stageWindows!(offer('next')); f.manager.consumer.commit(commit('next'));
 f.manager.prepare('a',{type:'message',text:'Describe',submission_id:'send'});
 f.manager.handle('a',{type:'gui_disconnected'});
 assert.equal(f.manager.get('a').pending?.status,'unknown');
 assert.deepEqual(f.releases,['capture']);
});

test('missing broker status is deduplicated and keystrokes respect reconnect backoff',()=>{
 let now=1000, inputs=0, active='a'; const events:any[]=[];
 const fake=Object.assign(new EventEmitter(), {state:{connection:'disconnected'},activityNS:0n,
   updateDraft(){},recordInput(){inputs++;},release(){}});
 const manager=new DesktopAppshot(()=>active,()=>true,(id,event)=>events.push({id,event}),()=>fake as any,()=>now);
 manager.activity(); fake.emit('notice','broker_unavailable');
 for(let i=0;i<50;i++){manager.activity();fake.emit('notice','broker_unavailable');}
 assert.equal(inputs,1); assert.equal(events.some(e=>e.event.type==='gui_notice'),false);
 assert.equal(events.filter(e=>e.event.notice==='broker_unavailable').length,1);
 now+=30_001; manager.activity(); assert.equal(inputs,2);
 active='b'; manager.update(); assert.equal(events.at(-1).id,'b');
 fake.state={connection:'connected'};fake.emit('change');
 assert.equal(events.at(-1).event.notice,''); manager.activity();assert.equal(inputs,3);
});

test('forgetting a closed runtime releases its local cache without revoking uncertain sent captures',()=>{
 const f=fixture(); f.manager.consumer.stageWindows!(offer()); f.manager.consumer.commit(commit());
 f.manager.prepare('a',{type:'message',text:'Describe',submission_id:'send'});
 f.select('b'); f.manager.forget('a');
 assert.equal(f.manager.get('a').pending,undefined); assert.equal(f.manager.capacity().appshotCount,0);
 assert.deepEqual(f.releases,[]);
});
