import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { Question } from '../src/renderer/controls.js';

test('pending question remains answerable while work continues',()=>{
  const html=renderToStaticMarkup(React.createElement(Question,{event:{type:'user_question_request',request_id:'q',state:'pending',mode:'timed',questions:[{id:'language',question:'中文还是英文？'}]},send:async()=>{}}));
  assert.match(html,/工作继续进行/);
  assert.match(html,/发送答复/);
  assert.match(html,/中文还是英文/);
});
