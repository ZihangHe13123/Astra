// Run: NODE_PATH=<jsdom installation>/node_modules node --test tests/test_browser_page.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const fs = require('node:fs');
const path = require('node:path');
function fixture(html, {insecure = false} = {}) {
  const dom = new JSDOM(html, {url:'https://example.org/', runScripts:'outside-only', pretendToBeVisual:true});
  // jsdom has no layout engine; supply nonzero geometry, retaining CSS visibility checks.
  dom.window.Element.prototype.getClientRects = function(){ return [{width:100,height:20}]; };
  if (insecure) Object.defineProperty(dom.window.crypto, 'randomUUID', {value:undefined});
  const source = path.join(__dirname,'../browser-control-extension/page.js');
  if(fs.existsSync(source)) dom.window.eval(fs.readFileSync(source,'utf8'));
  assert.equal(typeof dom.window.__astraBrowserPage,'function','shared page helper must exist');
  return dom.window;
}
test('bounded snapshot omits hidden and secret values, finds open shadow buttons', async()=>{
  const w=fixture('<title>Page</title><body><p>'+ 'x'.repeat(14000) +'</p><input type=password value=secret><input type=hidden value=hidden><button style="display:none">Invisible</button><div id=host></div><iframe></iframe></body>');
  w.document.querySelector('#host').attachShadow({mode:'open'}).innerHTML='<button>Shadow</button>';
  const s=await w.__astraBrowserPage('snapshot',{});
  assert.ok(s.text.length<=12000); assert.ok(!JSON.stringify(s).includes('secret'));
  assert.ok(!s.elements.some(e=>e.name==='Invisible')); assert.ok(s.elements.some(e=>e.name==='Shadow'));
  assert.ok(s.frames.length >= 2);
  w.close();
});

test('file controls under hidden containers remain discoverable without hiding a named form',async()=>{
  const w=fixture('<form><label>Visible<input id=visible type=file></label><div hidden><input id=hidden type=file multiple></div><input type=password value=secret></form>');
  const s=await w.__astraBrowserPage('snapshot',{scope:'form',role_filter:'file'});
  assert.equal(s.elements.length,2);assert.equal(s.elements[0].visible,true);assert.equal(s.elements[1].visible,false);
  assert.equal(s.elements[1].multiple,true);assert.ok(!JSON.stringify(s).includes('secret'));
  const r=await w.__astraBrowserPage('read',{ref:s.elements[1].ref});
  assert.deepEqual(Array.from(r.files),[]);assert.equal(r.value,undefined);
  assert.equal((await w.__astraBrowserPage('fill',{ref:s.elements[1].ref,text:'/a/file'})).status,'error');
  w.close();
});

function frameForm(w, id, html) {
  const frame=w.document.createElement('iframe');frame.id=id;w.document.body.append(frame);
  const doc=frame.contentDocument;doc.body.innerHTML=html;
  doc.defaultView.Element.prototype.getClientRects=function(){return [{width:100,height:20}]};
  return doc;
}

test('check batch selects ten independent radios with one compact observation and no repeated clicks',async()=>{
  const w=fixture(Array.from({length:10},(_,i)=>`<fieldset><legend>Question ${i}</legend><label><input type=radio name=q${i} id=r${i}>True</label></fieldset>`).join(''));
  let clicks=0;w.document.addEventListener('click',()=>clicks++);
  const initial=await w.__astraBrowserPage('snapshot',{role_filter:'radio'});
  const checks=initial.elements.map(e=>({selector:'ref:'+e.ref,checked:true}));
  const result=await w.__astraBrowserPage('check',{checks});
  assert.equal(result.status,'verified');assert.equal(result.completed,10);assert.equal(clicks,10);
  assert.equal(result.after.textIncluded,false);assert.ok(result.after.elements.every(e=>e.checked));
  const again=await w.__astraBrowserPage('check',{checks:result.after.elements.map(e=>({selector:'ref:'+e.ref,checked:true}))});
  assert.equal(again.status,'verified');assert.equal(again.clickCount,0);assert.equal(clicks,10);w.close();
});
test('check rejects invalid, disabled and conflicting targets before any dispatch',async()=>{
  const w=fixture('<input id=a type=radio name=q><input id=b type=radio name=q><input id=d type=checkbox disabled><button id=submit>Submit</button>');
  let clicks=0;w.document.addEventListener('click',()=>clicks++);
  for(const checks of [[{selector:'#a',checked:true},{selector:'#b',checked:true}],
    [{selector:'#a',checked:true},{selector:'#d',checked:true}],
    [{selector:'#submit',checked:true}],[{selector:'#a',checked:'true'}]]) {
    const r=await w.__astraBrowserPage('check',{checks});assert.notEqual(r.status,'verified');assert.equal(r.dispatch_state,'not_dispatched');
  }
  assert.equal(clicks,0);w.close();
});
test('check waits for delayed state, but never clicks a refusing target twice',async()=>{
  const w=fixture('<div role=checkbox aria-checked=false id=late>Late</div><div role=checkbox aria-checked=false id=no>No</div>');
  w.document.querySelector('#late').onclick=()=>w.setTimeout(()=>w.document.querySelector('#late').setAttribute('aria-checked','true'),30);
  assert.equal((await w.__astraBrowserPage('check',{selector:'#late',checked:true})).status,'verified');
  let count=0;w.document.querySelector('#no').onclick=()=>count++;
  const result=await w.__astraBrowserPage('check',{selector:'#no',checked:true});
  assert.equal(result.status,'verification_failed');assert.equal(result.completed,0);assert.equal(count,1);w.close();
});
test('check stops at removed or revoked target without replaying a successful prefix',async()=>{
  for(const revoke of [false,true]) {
    const w=fixture('<input id=a type=checkbox><input id=b type=checkbox>');
    let clicks=0;w.document.addEventListener('click',()=>clicks++);
    w.document.querySelector('#a').onclick=()=>{if(revoke) w.__astraBrowserPage('invalidate');else w.document.querySelector('#b').remove();};
    const result=await w.__astraBrowserPage('check',{checks:[{selector:'#a',checked:true},{selector:'#b',checked:true}]});
    assert.notEqual(result.status,'verified');assert.equal(clicks,1);assert.equal(result.completed,revoke?0:1);w.close();
  }
});
test('four same-label frame inputs are discovered, filled and read independently without focus',async()=>{
  const w=fixture('<p>Form</p>');
  const docs=Array.from({length:4},(_,i)=>frameForm(w,'q'+i,'<textarea aria-label="Answer"></textarea>'));
  w.focus=()=>{throw Error('must not focus window')};
  for(let i=0;i<4;i++) {
    const s=await w.__astraBrowserPage('snapshot',{scope:'editable'});
    assert.equal(s.elements.length,4);
    const frame=s.frames.find(f=>f.id==='q'+i);
    const el=s.elements.find(e=>e.frameRef===frame.frameRef);
    assert.equal(el.value,'');
    const filled=await w.__astraBrowserPage('fill',{ref:el.ref,text:'Answer '+i});
    assert.equal(filled.status,'verified');assert.equal(filled.value,'Answer '+i);
    assert.deepEqual(docs.map(d=>d.querySelector('textarea').value),Array.from({length:4},(_,j)=>j<=i?'Answer '+j:''));
    const fresh=filled.after.elements.find(e=>e.frameRef===filled.after.frames.find(f=>f.id==='q'+i).frameRef);
    const read=await w.__astraBrowserPage('read',{ref:fresh.ref});
    assert.equal(read.value,'Answer '+i);
  }
  assert.equal((await w.__astraBrowserPage('fill',{selector:'textarea',text:'bad'})).status,'ambiguous_target');
  w.close();
});
test('editable scope survives toolbar truncation and exposes values',async()=>{
  const w=fixture('<button>Tool</button>'.repeat(180)+'<input aria-label="Name" value="before">');
  const s=await w.__astraBrowserPage('snapshot',{scope:'editable'});
  assert.equal(s.elements.length,1);assert.equal(s.elements[0].value,'before');
  const result=await w.__astraBrowserPage('fill',{ref:s.elements[0].ref,text:'after'});
  assert.equal(result.verified,true);assert.equal(result.beforeValue,'before');
  w.close();
});
test('replacement and editor rejection never report verified',async()=>{
  const w=fixture('<input value="keep">');const el=w.document.querySelector('input');
  el.addEventListener('input',()=>{el.value='keep'});
  const r=await w.__astraBrowserPage('fill',{selector:'input',text:'wanted'});
  assert.equal(r.status,'verification_failed');assert.equal(r.value,'keep');
  let s=await w.__astraBrowserPage('snapshot');
  el.outerHTML='<input value="replacement">';
  assert.equal((await w.__astraBrowserPage('fill',{ref:s.elements[0].ref,text:'bad'})).status,'stale_snapshot');
  w.close();
});
test('detached iframe references never fall through to another frame',async()=>{
  const w=fixture('');frameForm(w,'q1','<input value="first">');frameForm(w,'q2','<input value="second">');
  const s=await w.__astraBrowserPage('snapshot',{scope:'editable'});w.document.querySelector('#q1').remove();
  const r=await w.__astraBrowserPage('fill',{ref:s.elements[0].ref,text:'bad'});
  assert.equal(r.status,'stale_snapshot');
  assert.equal(w.document.querySelector('#q2').contentDocument.querySelector('input').value,'second');w.close();
});
test('refs invalidate on resnapshot, replacement, URL changes, explicit invalidate',async()=>{
  const w=fixture('<button id=b>Go</button>'); const call=w.__astraBrowserPage;
  let s=await call('snapshot',{}); await call('snapshot',{});
  assert.equal((await call('click',{ref:s.elements[0].ref})).status,'stale_snapshot');
  s=await call('snapshot',{}); w.document.querySelector('button').outerHTML='<button id=b>Go</button>';
  assert.equal((await call('click',{ref:s.elements[0].ref})).status,'stale_snapshot');
  s=await call('snapshot',{}); w.history.pushState({},'','/changed');
  assert.equal((await call('click',{ref:s.elements[0].ref})).status,'stale_snapshot');
  s=await call('snapshot',{}); await call('invalidate',{});
  assert.equal((await call('click',{ref:s.elements[0].ref})).status,'stale_snapshot'); w.close();
});
test('unique visible enabled editable validation happens before any write',async()=>{
  const w=fixture('<button>A</button><button>B</button><button id=d disabled>D</button><input id=r readonly value=keep><input id=p type=password value=secret><input id=f type=file><div id=n>No</div><button id=h style="display:none">Hide</button>');
  for(const [op,args] of [['click',{selector:'button'}],['click',{selector:'#d'}],['click',{selector:'#h'}],['type',{selector:'#r',text:'x'}],['type',{selector:'#p',text:'x'}],['type',{selector:'#f',text:'x'}],['type',{selector:'#n',text:'x'}],['click',{selector:'#n',expectedOrigin:'https://other.org'}]])
    assert.equal((await w.__astraBrowserPage(op,args)).status,args.selector==='button'?'ambiguous_target':'error');
  assert.equal(w.document.querySelector('#r').value,'keep'); w.close();
});
test('click dispatched once and no-op distinguished from observed change',async()=>{
  const w=fixture('<button>Go</button><p>Before</p>'); let count=0;
  const b=w.document.querySelector('button'); b.addEventListener('click',()=>{count++});
  assert.equal((await w.__astraBrowserPage('click',{selector:'button'})).status,'no_observed_change'); assert.equal(count,1);
  b.addEventListener('click',()=>{w.document.querySelector('p').textContent='After'});
  const result=await w.__astraBrowserPage('click',{selector:'button'});
  assert.equal(result.status,'observed'); assert.ok(result.after.text.includes('After')); assert.equal(count,2); w.close();
});
test('type uses native value setter and events; select rejects absent/disabled options before mutation',async()=>{
  const w=fixture('<input id=i><select><option value=a>A</option><option value=b>B</option><option value=c disabled>C</option></select>');
  let events=[]; const i=w.document.querySelector('input');
  for(const e of ['input','change']) i.addEventListener(e,()=>events.push(e));
  assert.equal((await w.__astraBrowserPage('type',{selector:'#i',text:'Hello'})).status,'observed'); assert.equal(i.value,'Hello'); assert.deepEqual(events,['input','change']);
  assert.equal((await w.__astraBrowserPage('select',{selector:'select',value:'missing'})).status,'error'); assert.equal(w.document.querySelector('select').value,'a');
  assert.equal((await w.__astraBrowserPage('select',{selector:'select',value:'c'})).status,'error');
  assert.equal((await w.__astraBrowserPage('select',{selector:'select',value:'b'})).status,'observed'); w.close();
});
test('snapshot serialization bounded to 64 KiB even with Unicode labels',async()=>{
  const w=fixture('<p>'+ '🙂'.repeat(9000)+'</p>'+('<button>'+ '界'.repeat(300) +'</button>').repeat(151));
  const s=await w.__astraBrowserPage('snapshot',{});
  assert.ok(Buffer.byteLength(JSON.stringify(s))<=65536); assert.ok(s.elements.length<=150); w.close();
});
test('detach and reinsert invalidates old element identity',async()=>{
  const w=fixture('<button>Go</button>');const s=await w.__astraBrowserPage('snapshot',{});
  const el=w.document.querySelector('button');el.remove();w.document.body.append(el);
  assert.equal((await w.__astraBrowserPage('click',{ref:s.elements[0].ref})).status,'stale_snapshot');w.close();
});
test('post-dispatch exception is unknown and never retried',async()=>{
  const w=fixture('<button>Go</button>');let n=0;
  w.document.querySelector('button').click=()=>{n++;throw new Error('Lost context')};
  assert.equal((await w.__astraBrowserPage('click',{selector:'button'})).status,'unknown_outcome');assert.equal(n,1);w.close();
});
test('probe combines conditions and preserves snapshot refs',async()=>{
  const w=fixture('<button>Go</button><p>Ready</p>'); const s=await w.__astraBrowserPage('snapshot',{});
  const args={ref:s.elements[0].ref,text:'Ready',urlContains:'example.org'};
  assert.equal((await w.__astraBrowserPage('probe',args)).matched,true);
  assert.equal((await w.__astraBrowserPage('probe',{...args,text:'Missing'})).matched,false);
  assert.equal((await w.__astraBrowserPage('probe',{selector:'.missing'})).matched,false);
  assert.equal((await w.__astraBrowserPage('click',{ref:s.elements[0].ref})).status,'no_observed_change');w.close();
});
test('probe rejects stale refs and ambiguous selectors without dispatch',async()=>{
  const w=fixture('<button>Go</button><button>Other</button>'); const s=await w.__astraBrowserPage('snapshot',{});
  assert.equal((await w.__astraBrowserPage('probe',{selector:'button'})).status,'ambiguous_target');
  await w.__astraBrowserPage('invalidate',{});
  assert.equal((await w.__astraBrowserPage('probe',{ref:s.elements[0].ref})).status,'stale_snapshot');
  assert.equal((await w.__astraBrowserPage('probe',{})).status,'error');w.close();
});

test('page helper keeps unpredictable, isolated refs without secure-context randomUUID', async()=>{
  const first=fixture('<button>Go</button>', {insecure:true});
  const second=fixture('<button>Go</button>', {insecure:true});
  try {
    const a=await first.__astraBrowserPage('snapshot',{});
    const b=await second.__astraBrowserPage('snapshot',{});
    assert.notEqual(a.elements[0].ref,b.elements[0].ref);
    assert.equal((await second.__astraBrowserPage('click',{ref:a.elements[0].ref})).status,'stale_snapshot');
    assert.equal((await first.__astraBrowserPage('click',{ref:a.elements[0].ref})).status,'no_observed_change');
  } finally {first.close();second.close();}
});

test('nested frame, scoped pagination, readonly and iframe document replacement',async()=>{
  const w=fixture('<label for="top">Top</label><input id="top">');
  const doc=frameForm(w,'outer','<h2>Outer</h2>');
  const nested=doc.createElement('iframe');nested.id='inner';doc.body.append(nested);
  nested.contentDocument.body.innerHTML='<input value="nested"><input readonly value="keep">';
  nested.contentWindow.Element.prototype.getClientRects=function(){return [{}]};
  const s=await w.__astraBrowserPage('snapshot',{scope:'editable'});
  assert.equal(s.elements.length,3);const f=s.frames.find(f=>f.id==='inner');
  let page=await w.__astraBrowserPage('snapshot',{scope:'editable',frame_ref:f.frameRef,limit:1});
  assert.equal(page.elements[0].value,'nested');assert.equal(page.nextOffset,1);
  const old=page.elements[0].ref;
  page=await w.__astraBrowserPage('snapshot',{scope:'editable',frame_ref:f.frameRef,offset:1,limit:1});
  assert.equal(page.elements[0].readonly,true);
  assert.equal((await w.__astraBrowserPage('fill',{ref:page.elements[0].ref,text:'bad'})).status,'error');
  assert.equal((await w.__astraBrowserPage('fill',{ref:old,text:'bad'})).status,'stale_snapshot');
  const current=await w.__astraBrowserPage('snapshot',{scope:'editable',frame_ref:f.frameRef});
  nested.contentDocument.open();nested.contentDocument.write('<body><input value="replacement"></body>');nested.contentDocument.close();
  assert.equal((await w.__astraBrowserPage('fill',{ref:current.elements[0].ref,text:'bad'})).status,'stale_snapshot');
  assert.equal(nested.contentDocument.querySelector('input').value,'replacement');w.close();
});

test('four independent rich editors stay isolated over twenty rounds',async()=>{
  const w=fixture('');const docs=Array.from({length:4},(_,i)=>frameForm(w,'editor'+i,'<div contenteditable="true" tabindex="0" aria-label="Answer"></div>'));
  // jsdom has no native editing commands. Model the command boundary, including
  // cancellation, separately from the real Chromium/TinyMCE acceptance fixture.
  for(const doc of docs) doc.execCommand=(command,_ui,text)=>{
    const el=doc.activeElement;assert.equal(el,doc.querySelector('[contenteditable]'));
    assert.ok(el.contains(doc.getSelection().anchorNode));if(command==='delete')el.textContent='';else {const parsed=doc.createElement('div');parsed.innerHTML=text;el.textContent=parsed.innerHTML.replace(/<br>/g,'\n').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');}
    el.dispatchEvent(new doc.defaultView.InputEvent('input',{bubbles:true,inputType:'insertText',data:text}));return true;
  };
  const expected=Array(4).fill('');
  for(let round=0;round<20;round++) for(let i=0;i<4;i++) {
    const s=await w.__astraBrowserPage('snapshot',{scope:'editable'});
    const target=s.elements.find(e=>e.frameRef===s.frames.find(f=>f.id==='editor'+i).frameRef);
    expected[i]=round===19?'':'第 '+round+' 轮 / '+i+'\nDifferent answer';
    const result=await w.__astraBrowserPage('fill',{ref:target.ref,text:expected[i]});
    assert.equal(result.verified,true,JSON.stringify(result));
    assert.deepEqual(docs.map(d=>d.querySelector('[contenteditable]').textContent),expected);
  }
  w.close();
});

test('concurrent helper fills serialize and revoke queued refs',async()=>{
  const w=fixture('<input id=a><input id=b>');
  const s=await w.__astraBrowserPage('snapshot',{scope:'editable'});
  const [a,b]=await Promise.all([
    w.__astraBrowserPage('fill',{ref:s.elements[0].ref,text:'A'}),
    w.__astraBrowserPage('fill',{ref:s.elements[1].ref,text:'B'})
  ]);
  assert.equal(a.verified,true);assert.equal(b.status,'stale_snapshot');
  assert.equal(w.document.querySelector('#b').value,'');w.close();
});

test('cross-origin and opaque sandbox frames are reported and excluded',async()=>{
 const w=fixture('');const good=frameForm(w,'good','<input>');
 const sandbox=frameForm(w,'opaque','<input value="private">');w.document.querySelector('#opaque').setAttribute('sandbox','allow-scripts');
 const foreign=frameForm(w,'foreign','<input value="other-origin">');
 // jsdom lacks SOP; simulate a Document from a different origin and verify our
 // explicit origin check in addition to the renderer's access restriction.
 Object.defineProperty(foreign,'URL',{value:'https://other.example/form'});
 const s=await w.__astraBrowserPage('snapshot',{scope:'editable'});
 assert.equal(s.elements.length,1);assert.ok(s.limitations.some(x=>x.includes('opaque')));assert.ok(s.limitations.some(x=>x.includes('foreign')));
 assert.ok(!JSON.stringify(s).includes('other-origin'));assert.ok(!JSON.stringify(s).includes('private'));
 assert.equal((await w.__astraBrowserPage('fill',{selector:'input',text:'only allowed'})).verified,true);
 assert.equal(good.querySelector('input').value,'only allowed');assert.equal(sandbox.querySelector('input').value,'private');w.close();
});

test('frame URL generation expires frame-scoped snapshots',async()=>{
 const w=fixture('');const doc=frameForm(w,'frame','<input>');
 const s=await w.__astraBrowserPage('snapshot',{scope:'editable'});const frame=s.frames.find(f=>f.id==='frame');
 Object.defineProperty(doc,'URL',{value:'https://example.org/replaced'});
 const r=await w.__astraBrowserPage('snapshot',{frame_ref:frame.frameRef});
 assert.equal(r.status,'stale_snapshot');w.close();
});

test('native checked state is independent of value and follows checkbox and radio changes',async t=>{
  const w=fixture('<input id=c type=checkbox value=1><input id=a type=radio name=group value=1 checked><input id=b type=radio name=group value=1><input id=text value=1>');
  t.after(()=>w.close());
  const call=w.__astraBrowserPage;
  w.document.querySelector('#c').indeterminate=true;
  let s=await call('snapshot',{});
  const find=id=>s.elements.find(e=>e.id===id);
  assert.equal(find('c').checked,false);assert.equal(find('c').indeterminate,true);
  assert.equal(find('a').checked,true);assert.equal(find('b').checked,false);
  assert.ok(!('checked' in find('text')));assert.ok(!('indeterminate' in find('a')));
  let r=await call('read',{ref:find('c').ref});
  assert.equal(r.value,'1');assert.equal(r.target.checked,false);assert.equal(r.target.indeterminate,true);
  r=await call('click',{ref:find('c').ref});s=r.after;
  assert.equal(r.status,'observed');assert.equal(find('c').checked,true);assert.equal(find('c').indeterminate,false);
  r=await call('read',{ref:find('c').ref});assert.equal(r.value,'1');assert.equal(r.target.checked,true);
  s=(await call('click',{ref:find('c').ref})).after;assert.equal(find('c').checked,false);
  s=(await call('click',{ref:find('b').ref})).after;
  assert.equal(find('a').checked,false);assert.equal(find('b').checked,true);
  w.document.querySelector('#b').checked=false;
  assert.equal((await call('read',{ref:find('b').ref})).target.checked,false);
});

test('ARIA checked state preserves mixed and unknown and observes attribute-only changes',async t=>{
  const w=fixture('<div id=c role=checkbox aria-checked=mixed>Choice</div><div id=r role=radio aria-checked=mixed>Radio</div><div id=missing role=checkbox>Missing</div><div id=invalid role=checkbox aria-checked=invalid>Invalid</div><button id=plain aria-checked=true>Plain</button>');
  t.after(()=>w.close());const call=w.__astraBrowserPage;
  let s=await call('snapshot',{});
  const find=id=>s.elements.find(e=>e.id===id);
  assert.equal(find('c').checked,'mixed');assert.equal(find('r').checked,null);
  assert.equal(find('missing').checked,null);assert.equal(find('invalid').checked,null);
  assert.ok(!('checked' in find('plain')));
  w.document.querySelector('#c').addEventListener('click',e=>e.currentTarget.setAttribute('aria-checked','true'));
  const r=await call('click',{ref:find('c').ref});s=r.after;
  assert.equal(r.status,'observed');assert.equal(find('c').checked,true);
  w.document.querySelector('#r').setAttribute('aria-checked','false');
  assert.equal((await call('read',{ref:find('r').ref})).target.checked,false);
});

test('compact snapshot reduces representative form payload and retains values, states and fresh refs',async t=>{
  const w=fixture('<title>Seven question form</title><p>'+('Read the question and choose all applicable answers. '.repeat(135))+'</p>'+Array.from({length:7},(_,i)=>'<label><input id=c'+i+' type=checkbox value=1>Option '+i+'</label>').join(''));
  t.after(()=>w.close());const call=w.__astraBrowserPage;
  frameForm(w,'q1','<textarea aria-label="Answer">First</textarea>');
  frameForm(w,'q2','<textarea aria-label="Answer">Second</textarea>');
  const full=await call('snapshot',{});
  assert.equal(full.textIncluded,true);assert.ok(full.text.length>6000);
  let s=await call('snapshot',{include_text:false});
  assert.equal(s.textIncluded,false);assert.equal(s.text,'');
  const withoutRefs=s=>JSON.parse(JSON.stringify(s.elements.map(({ref,...rest})=>rest)));
  assert.deepEqual(withoutRefs(s),withoutRefs(full));assert.deepEqual(s.frames,full.frames);
  assert.deepEqual(s.limitations,full.limitations);assert.equal(s.totalMatches,full.totalMatches);
  const fullBytes=Buffer.byteLength(JSON.stringify(full)),compactBytes=Buffer.byteLength(JSON.stringify(s));
  assert.ok(compactBytes<=fullBytes*0.6,`${compactBytes} vs ${fullBytes}`);
  t.diagnostic(`representative form: full=${fullBytes} bytes, compact=${compactBytes} bytes, reduction=${(100*(1-compactBytes/fullBytes)).toFixed(1)}%`);
  assert.equal((await call('fill',{ref:full.elements[0].ref,text:'stale'})).status,'stale_snapshot');
  for(const [id,text] of [['q1','First changed'],['q2','Second changed']]) {
    const target=s.elements.find(e=>e.frameRef===s.frames.find(f=>f.id===id).frameRef);
    const r=await call('fill',{ref:target.ref,text});
    assert.equal(r.verified,true);assert.equal(r.value,text);s=r.after;
    assert.equal(s.text,'');assert.equal(s.textIncluded,false);
  }
  const clicked=await call('click',{ref:s.elements.find(e=>e.id==='c4').ref});s=clicked.after;
  assert.equal(clicked.status,'observed');assert.equal(s.textIncluded,false);
  assert.equal(s.elements.find(e=>e.id==='c4').checked,true);
  assert.equal((await call('probe',{text:'Read the question'})).matched,true);
  const r=await call('read',{ref:s.elements.find(e=>e.id==='c4').ref});assert.equal(r.target.checked,true);
  s=await call('snapshot',{include_text:true});assert.equal(s.textIncluded,true);assert.ok(s.text.includes('Read the question'));
  assert.ok(s.elements.some(e=>e.value==='First changed'));assert.ok(s.elements.some(e=>e.value==='Second changed'));
  assert.ok((await call('click',{ref:s.elements.find(e=>e.id==='c4').ref})).after.text.length>6000);
});

test('compact snapshot keeps scoped pagination and frame fallback, and rejects invalid options before invalidating refs',async t=>{
  const w=fixture('<p>Top page</p><input id=top>');t.after(()=>w.close());const call=w.__astraBrowserPage;
  const doc=frameForm(w,'frame','<button id=remove>Remove frame</button><button>Keep</button>');
  let s=await call('snapshot',{});const f=s.frames.find(f=>f.id==='frame');
  s=await call('snapshot',{frame_ref:f.frameRef,role_filter:'button',limit:1,include_text:false});
  assert.equal(s.totalMatches,2);assert.equal(s.nextOffset,1);assert.equal(s.elements.length,1);
  assert.equal(s.elements[0].frameRef,f.frameRef);assert.equal(s.textIncluded,false);
  for(const include_text of [null,'false',0,{}]) assert.equal((await call('snapshot',{include_text})).status,'error');
  doc.querySelector('#remove').onclick=()=>w.document.querySelector('iframe').remove();
  const r=await call('click',{ref:s.elements[0].ref});
  assert.equal(r.status,'observed');assert.equal(r.after.textIncluded,false);assert.equal(r.after.text,'');
  assert.equal(r.after.scope,'all');assert.ok(r.after.elements.some(e=>e.id==='top'));
  assert.ok(!r.after.frames.some(frame=>frame.frameRef===f.frameRef));
});

test('compact observation settings do not leak to another page or survive invalidation',async t=>{
  const a=fixture('<button>First page</button>'),b=fixture('<button>Second page</button>');
  t.after(()=>{a.close();b.close()});
  await a.__astraBrowserPage('snapshot',{include_text:false});
  assert.equal((await b.__astraBrowserPage('snapshot',{})).textIncluded,true);
  await a.__astraBrowserPage('invalidate',{});
  const r=await a.__astraBrowserPage('click',{selector:'button'});
  assert.equal(r.after.textIncluded,true);assert.ok(r.after.text.includes('First page'));
});

test('verification yields without background-throttled page timers',async()=>{
 const w=fixture('<input>');
 w.MessageChannel=require('node:worker_threads').MessageChannel;
 w.setTimeout=()=>{throw Error('page timers must not gate verification')};
 const el=w.document.querySelector('input');
 el.addEventListener('input',()=>{w.queueMicrotask(()=>{el.value='app rejected'})});
 const r=await w.__astraBrowserPage('fill',{selector:'input',text:'wanted'});
 assert.equal(r.status,'verification_failed');assert.equal(r.value,'app rejected');w.close();
});

test('snapshot says whether visible page text went past the 12000-character limit',async t=>{
  const long=fixture('<p>'+'a'.repeat(11990)+'</p><p>tail that does not fit</p>');t.after(()=>long.close());
  let s=await long.__astraBrowserPage('snapshot',{});
  assert.equal(s.textTruncated,true);assert.equal(s.text.length,12000);assert.ok(!s.text.includes('does not fit'));
  // Without page text there is nothing to cut, so the flag is left out.
  s=await long.__astraBrowserPage('snapshot',{include_text:false});
  assert.equal(s.text,'');assert.ok(!('textTruncated' in s));
  // The wait probe searches the same window.
  assert.equal((await long.__astraBrowserPage('probe',{text:'does not fit'})).matched,false);
  const short=fixture('<p>'+'a'.repeat(11990)+'</p><p hidden>hidden text is not page text</p>');t.after(()=>short.close());
  s=await short.__astraBrowserPage('snapshot',{});
  assert.equal(s.textTruncated,false);assert.equal(s.text.length,11990);
});

test('read continues a long value from an offset without touching refs',async t=>{
  const body='0123456789'.repeat(3000);
  const w=fixture('<button>Go</button><div id=c>'+body+'</div>');t.after(()=>w.close());const call=w.__astraBrowserPage;
  const s=await call('snapshot',{});
  let r=await call('read',{selector:'#c'});
  assert.equal(r.value,body.slice(0,12000));assert.equal(r.valueTruncated,true);
  assert.equal(r.valueOffset,0);assert.equal(r.valueLength,30000);
  r=await call('read',{selector:'#c',offset:12000});
  assert.equal(r.value,body.slice(12000,24000));assert.equal(r.valueTruncated,true);assert.equal(r.valueOffset,12000);
  r=await call('read',{selector:'#c',offset:24000});
  assert.equal(r.value,body.slice(24000));assert.equal(r.valueTruncated,false);
  r=await call('read',{selector:'#c',offset:30000});
  assert.equal(r.value,'');assert.equal(r.valueTruncated,false);
  for(const offset of [-1,1.5,'12000']) assert.equal((await call('read',{selector:'#c',offset})).status,'error');
  assert.equal((await call('click',{ref:s.elements[0].ref})).status,'no_observed_change');
});

test('select lists its options, accepts one exact visible label, and changes nothing on a miss',async t=>{
  const w=fixture('<select id=s><option value="">Choose</option><option value=sg>Singapore</option><option value=my>  Malaysia\n</option>'+
    '<option value=x1>Other</option><option value=x2>Other</option><option value=no disabled>Norway</option><option value=Other>Value wins</option></select>');
  t.after(()=>w.close());const el=w.document.querySelector('select');
  // Results come from the page's realm; compare them as plain data.
  const call=async(...args)=>JSON.parse(JSON.stringify(await w.__astraBrowserPage(...args)));
  let events=0;el.addEventListener('change',()=>events++);
  const listed=(await call('snapshot',{})).elements.find(e=>e.id==='s');
  assert.deepEqual(listed.options.slice(0,3),[{value:'',label:'Choose',selected:true},{value:'sg',label:'Singapore'},{value:'my',label:'Malaysia'}]);
  assert.deepEqual(listed.options[5],{value:'no',label:'Norway',disabled:true});
  assert.deepEqual((await call('read',{selector:'#s'})).options,listed.options);
  // No value and no label: the failure carries the choices and the page is untouched.
  let r=await call('select',{selector:'#s',value:'Japan'});
  assert.equal(r.status,'error');assert.deepEqual(r.options,listed.options);assert.equal(el.value,'');assert.equal(events,0);
  // A disabled option stays unselectable by value or by label.
  for(const value of ['no','Norway']) {r=await call('select',{selector:'#s',value});assert.equal(r.status,'error');assert.ok(r.options.length);}
  assert.equal(el.value,'');assert.equal(events,0);
  // One option with this visible label.
  r=await call('select',{selector:'#s',value:'Malaysia'});
  assert.equal(r.status,'observed');assert.equal(el.value,'my');assert.deepEqual(r.option,{value:'my',label:'Malaysia',matchedBy:'label'});
  assert.equal(r.after.elements.find(e=>e.id==='s').options[2].selected,true);
  // A value match is preferred to a label shared by two other options.
  r=await call('select',{selector:'#s',value:'Other'});
  assert.equal(r.status,'observed');assert.equal(el.value,'Other');assert.equal(r.option.matchedBy,'value');
  assert.equal(events,2);
  w.document.querySelector('option[value=Other]').remove();
  r=await call('select',{selector:'#s',value:'Other'});
  assert.equal(r.status,'error');assert.match(r.message,/several options share this label/);assert.equal(events,2);
  // Refs work as selectors too.
  const fresh=(await call('snapshot',{})).elements.find(e=>e.id==='s');
  assert.equal((await call('select',{selector:'ref:'+fresh.ref,value:'sg'})).status,'observed');assert.equal(el.value,'sg');
});

test('select keeps the matched option when another option shares its value',async t=>{
  const w=fixture('<select id=s><option value="">Choose</option><option value=dup>First</option><option value=dup>Second</option></select>');
  t.after(()=>w.close());
  const r=await w.__astraBrowserPage('select',{selector:'#s',value:'Second'});
  assert.equal(r.status,'observed');assert.equal(w.document.querySelector('select').selectedIndex,2);
});

test('option lists are bounded in snapshots, reads and failures',async t=>{
  const w=fixture('<select id=s>'+Array.from({length:130},(_,i)=>`<option value="v${i}">${'Label '+i+' '+'x'.repeat(200)}</option>`).join('')+'</select>');
  t.after(()=>w.close());const call=w.__astraBrowserPage;
  const listed=(await call('snapshot',{})).elements.find(e=>e.id==='s');
  assert.equal(listed.options.length,20);assert.equal(listed.optionsTruncated,true);assert.equal(listed.optionCount,130);
  assert.ok(listed.options.every(o=>o.label.length<=120));
  for(const r of [await call('read',{selector:'#s'}),await call('select',{selector:'#s',value:'missing'})]) {
    assert.equal(r.options.length,100);assert.equal(r.optionsTruncated,true);assert.equal(r.optionCount,130);
  }
});
