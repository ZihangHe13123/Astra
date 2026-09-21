import test from 'node:test';
import assert from 'node:assert/strict';
import { toolEmptyOutput, toolExpanded, toolStatus } from '../src/renderer/tool-status.js';
import { hasOngoingWork } from '../src/main/ongoing-work.js';
import { initialSession, projectEvent } from '@astra/ui-core/session-state';

test('failed and successful empty receipts never claim to be waiting', () => {
 const failure={type:'tool_result',error:'Permission denied',output:''};
 assert.equal(toolStatus(failure),'failed');assert.equal(toolEmptyOutput(failure),'');
 assert.equal(toolEmptyOutput({type:'tool_result',output:''}),'已完成，无文本输出。');
 assert.equal(toolEmptyOutput({type:'tool_calls',status:'running'}),'等待结果…');
 assert.doesNotMatch(toolEmptyOutput({type:'tool_calls',status:'interrupted'}),/等待/);
 assert.equal(toolExpanded({...failure,historical:true}),false);assert.equal(toolExpanded(failure),true);
});

test('background delegates count as ongoing work until independently settled', () => {
 let s=projectEvent(initialSession('a'),{type:'gui_ready'});
 s=projectEvent(s,{type:'delegate_status',process_id:'child',status:'queued'});
 assert.equal(hasOngoingWork(s),true);
 s=projectEvent(s,{type:'task_status',task:{status:'completed'}});assert.equal(hasOngoingWork(s),true);
 s=projectEvent(s,{type:'delegate_status',process_id:'child',status:'cancelled'});assert.equal(hasOngoingWork(s),false);
});
