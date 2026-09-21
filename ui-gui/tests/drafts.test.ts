import {test} from 'node:test';
import assert from 'node:assert/strict';
import {migrateBlankDraft} from '../src/renderer/drafts.js';
const base:any={drafts:{new:'未发送'},attachments:{new:['/tmp/a']}};
test('upgrading blank draft keys retains text and attachments without modifying the input',()=>{
 const next=migrateBlankDraft(base,'/work');
 assert.equal(next.drafts['new:/work'],'未发送');assert.deepEqual(next.attachments['new:/work'],['/tmp/a']);
 assert.equal(base.drafts.new,'未发送');
 assert.equal(migrateBlankDraft({...base,drafts:{new:'old','new:/work':'new'}},'/work').drafts['new:/work'],'new');
});
