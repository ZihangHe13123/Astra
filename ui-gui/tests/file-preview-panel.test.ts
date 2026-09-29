import {test} from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {DocumentPreviewBody} from '../src/renderer/file-preview-panel.js';

test('spreadsheet preview names saved formula state and structural check separately',()=>{
 const html = renderToStaticMarkup(React.createElement(DocumentPreviewBody,{preview:{path:'/x/a.xlsx',kind:'spreadsheet',checks:{format:'xlsx',status:'passed',parts:4,warnings:[]},warnings:['未重新计算公式'],sheets:[{name:'Sheet 1',rows:[['<unsafe>','3']],truncated:true}]},runtime:'r',fail:()=>{}}));
 assert.match(html,/结构检查通过/); assert.match(html,/未重新计算公式/); assert.match(html,/&lt;unsafe&gt;/); assert.match(html,/前 200 行/);
});

test('PDF preview reports missing fonts and avoids treating conversion as layout verification',()=>{
 const html=renderToStaticMarkup(React.createElement(DocumentPreviewBody,{preview:{path:'/x/a.docx',kind:'pdf',data:'data:application/pdf;base64,JVBERi0=',converted:true,missingFonts:['Missing Sans'],checks:{format:'docx',status:'passed',parts:4,warnings:[]}},runtime:'r',fail:()=>{}}));
 assert.match(html,/Missing Sans/); assert.match(html,/未进行版式验收/); assert.match(html,/PDF/);
});
