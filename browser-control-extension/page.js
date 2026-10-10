/* Inject only into an isolated world. No page-supplied code is evaluated. */
(() => {
  'use strict';
  const VERSION = 11;
  if (globalThis.__astraBrowserPageVersion === VERSION) return;
  globalThis.__astraBrowserPage?.('invalidate', {});
  globalThis.__astraBrowserPageVersion = VERSION;
  const session = Array.from(crypto.getRandomValues(new Uint8Array(16)),
    byte => byte.toString(16).padStart(2, '0')).join('');
  let sequence = 0, frameSequence = 0, generation = 0, refs = new Map(), lastOptions = {};
  const documentIds = new WeakMap();
  const parent = el => el.parentElement || el.getRootNode()?.host;
  const interactive = 'a[href],button,input,textarea,select,[role],[tabindex],[contenteditable]';
  let uploadEngine;
  const fileInput = el => el.tagName === 'INPUT' && el.type === 'file';
  const fileInfo = el => fileInput(el) ? {accept:el.accept.slice(0,2000),multiple:el.multiple,webkitdirectory:!!el.webkitdirectory,visible:visible(el),files:Array.from(el.files || []).slice(0,10).map(f=>({name:f.name.slice(0,255),size:f.size,type:f.type.slice(0,127)})),filesTruncated:(el.files?.length || 0)>10} : {};
  const secret = el => el.tagName === 'INPUT' && ['password','file','hidden'].includes(el.type);
  const contentEditable = el => el.isContentEditable || ['','true','plaintext-only'].includes(el.getAttribute('contenteditable'));
  const editable = el => contentEditable(el) || el.tagName === 'TEXTAREA' ||
    (el.tagName === 'INPUT' && ['text','search','email','url','tel','number'].includes(el.type));
  function value(el) {
    if(contentEditable(el)) {
      // TinyMCE represents an empty editor with a single bogus BR in a block.
      const placeholder=el.querySelector('br[data-mce-bogus]');
      if(placeholder && !el.textContent && el.querySelectorAll('br').length===1 && el.children.length===1) return '';
      return (el.innerText ?? el.textContent).replace(/\r\n?/g,'\n');
    }
    return (el.value ?? el.innerText ?? el.textContent ?? '').replace(/\r\n?/g,'\n');
  }
  const failure = (status, message) => Object.assign(new Error(message), {status});
  const optionLabel = option => (option.label || option.textContent || '').replace(/\s+/g,' ').trim();
  const optionUsable = option => !option.disabled && !option.parentElement?.disabled;
  function selectOptions(el, limit) {
    if(el.tagName!=='SELECT') return {};
    const all=[...el.options];
    return {options:all.slice(0,limit).map(o=>({value:o.value.slice(0,120),label:optionLabel(o).slice(0,120),
      ...(o.selected ? {selected:true} : {}),...(optionUsable(o) ? {} : {disabled:true})})),
      ...(all.length>limit ? {optionsTruncated:true,optionCount:all.length} : {})};
  }
  const opaqueSandbox = el => el.hasAttribute('sandbox') && !el.getAttribute('sandbox').split(/\s+/).includes('allow-same-origin');
  function allowedDocument(doc) {
    // DOM access alone is insufficient on pages that relax document.domain.
    // A normal child URL must retain the exact approved origin.
    void doc.defaultView.location.href;
    const url=new URL(doc.URL);
    return url.origin===location.origin || (url.protocol==='about:' && ['blank','srcdoc'].includes(url.pathname));
  }
  function visible(el) {
    if (!el.isConnected || !el.getClientRects().length) return false;
    for (let node = el; node; node = parent(node)) {
      const style = node.ownerDocument.defaultView.getComputedStyle(node);
      if (node.hasAttribute('hidden') || style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse' || style.opacity === '0') return false;
    }
    return true;
  }
  function disabled(el) {
    for (let node = el; node; node = parent(node))
      if (node.matches(':disabled') || node.inert === true || node.hasAttribute('inert') || node.getAttribute('aria-disabled') === 'true') return true;
    return false;
  }
  function validFrame(frame) {
    try {
      if (frame.doc.documentElement !== frame.root || frame.doc.URL !== frame.url) return false;
      if (!frame.owner) return frame.doc === document;
      return frame.owner.isConnected && frame.owner.contentDocument === frame.doc &&
        !opaqueSandbox(frame.owner) && allowedDocument(frame.doc) && validFrame(frame.parent);
    } catch {return false;}
  }
  function validTarget(target) {
    return target.el.isConnected && target.el.ownerDocument === target.frame.doc && validFrame(target.frame);
  }
  function fileReadbackAnchor(el) {
    const root=el.getRootNode(), id=el.id, escape=el.ownerDocument.defaultView.CSS?.escape;
    if(!id || id.length>255 || !escape) return null;
    const selector='#'+escape(id), found=root.querySelectorAll(selector);
    if(found.length!==1 || found[0]!==el) return null;
    return {root,selector,form:el.form,name:el.name,accept:el.accept,multiple:el.multiple,
      directory:el.hasAttribute('webkitdirectory')};
  }
  function clearedFileReplacement(target) {
    // Only observe the same logical field after a clear event rebuilt it.
    // This neither restores the old action ref nor grants input to a new node.
    const anchor=target.fileReadbackAnchor;
    if(target.generation!==generation || !validFrame(target.frame) || target.el.isConnected ||
       !anchor || !anchor.root.isConnected || (anchor.form && !anchor.form.isConnected)) return null;
    const found=anchor.root.querySelectorAll(anchor.selector), el=found[0];
    if(found.length!==1 || !fileInput(el) || el.ownerDocument!==target.frame.doc || disabled(el) ||
       el.form!==anchor.form || el.name!==anchor.name || el.accept!==anchor.accept ||
       el.multiple!==anchor.multiple || el.hasAttribute('webkitdirectory')!==anchor.directory) return null;
    return el;
  }
  function forgetRemoved(records) {
    for (const record of records) for (const removed of record.removedNodes) {
      for (const [ref, target] of refs) {
        const nodes = [target.el];
        for (let frame = target.frame; frame.owner; frame = frame.parent) nodes.push(frame.owner);
        if (nodes.some(el => {
          for (let node=el; node; node=parent(node)) if (node === removed || removed.contains(node)) return true;
          return false;
        })) refs.delete(ref);
      }
    }
  }
  const observer = new MutationObserver(forgetRemoved);
  function name(el, limit=200) {
    const root = el.getRootNode();
    const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => root.getElementById?.(id)?.textContent || '').join(' ').trim();
    const label = labelled || el.getAttribute('aria-label') || (el.labels ? [...el.labels].filter(visible).map(x=>x.textContent).join(' ') : '') || el.getAttribute('alt') || el.getAttribute('title') || el.getAttribute('placeholder') || (!editable(el) ? el.innerText || el.textContent : '') || '';
    return label.replace(/\s+/g,' ').trim().slice(0,limit);
  }
  function role(el) {
    return (fileInput(el) ? 'file' : el.getAttribute('role')) || (editable(el) ? 'textbox' : ({BUTTON:'button',A:'link',SELECT:'combobox',INPUT:['checkbox','radio'].includes(el.type)?el.type:'input'}[el.tagName])) || el.tagName.toLowerCase();
  }
  function context(el) {
    for (let node=parent(el), depth=0; node && depth++<6; node=parent(node)) {
      const heading = node.querySelector('h1,h2,h3,h4,h5,h6,legend');
      if (heading && visible(heading)) return heading.textContent.replace(/\s+/g,' ').trim().slice(0,200);
    }
    return '';
  }
  function scan() {
    const elements=[], allFrames=[], roots=[], chunks=[], limitations=['Only open shadow roots and accessible same-origin frames are inspected.'];
    let count=0, length=0, textTruncated=false;
    function addDocument(doc, owner=null, enclosing=null, depth=0) {
      if (allFrames.length>=32 || depth>8) {limitations.push('Frame traversal limit reached (32 frames, depth 8).');return;}
      let identity=documentIds.get(doc);
      if(!identity || identity.root!==doc.documentElement || identity.url!==doc.URL) {
        identity={root:doc.documentElement,url:doc.URL,ref:session+':f'+(++frameSequence)};
        documentIds.set(doc,identity);
      }
      const frame={doc,owner,parent:enclosing,root:doc.documentElement,url:doc.URL,frameRef:identity.ref};
      allFrames.push(frame);roots.push(doc);
      walk(doc.body || doc.documentElement, frame, depth);
    }
    function walk(node, frame, depth) {
      if (!node || ++count>20000) return;
      if (node.nodeType===3) {
        // After the limit, look only far enough to learn whether visible text was left out.
        if(!textTruncated && node.parentElement) {
          const full=node.textContent.replace(/\s+/g,' ').trim();
          if(full && visible(node.parentElement)) {
            const text=full.slice(0,Math.max(0,12000-length));
            if(text){chunks.push(text);length+=text.length+1;}
            if(text.length<full.length) textTruncated=true;
          }
        }
        return;
      }
      if (node.nodeType!==1 || ['SCRIPT','STYLE','NOSCRIPT','TEMPLATE'].includes(node.tagName) || (secret(node) && !fileInput(node))) return;
      if (fileInput(node)) elements.push({el:node,frame});
      else if (visible(node) && node.matches(interactive) && !['presentation','none'].includes(role(node))) elements.push({el:node,frame});
      if (visible(node) && node.matches('iframe,frame')) {
        try {
          if(opaqueSandbox(node)) throw new Error('opaque sandbox');
          const doc=node.contentDocument;
          // Access to this Document is checked by the browser's same-origin policy,
          // including inherited-origin about:blank/srcdoc. Never inject into other origins.
          if(!doc || !doc.documentElement) throw new Error('inaccessible frame');
          if(!allowedDocument(doc)) throw new Error('different origin');
          addDocument(doc,node,frame,depth+1);
        } catch {limitations.push('Frame '+(node.id || node.name || node.title || '(unnamed)').slice(0,120)+': cross-origin, sandboxed or not loaded; content not inspected.');}
      }
      const root=node.shadowRoot || node;
      if(node.shadowRoot) roots.push(root);
      for(const child of root.childNodes) {if(count>=20000)break;walk(child,frame,depth);}
    }
    addDocument(document);
    if(count>=20000) limitations.push('DOM traversal truncated at 20000 nodes.');
    return {elements,frames:allFrames,roots,text:chunks.join('\n').slice(0,12000),textTruncated,limitations:[...new Set(limitations)]};
  }
  function frameInfo(frame) {
    return {frameRef:frame.frameRef,parentFrameRef:frame.parent?.frameRef || '',url:frame.url.slice(0,4096),
      id:frame.owner?.id.slice(0,200) || '',name:frame.owner?.name?.slice(0,200) || '',title:frame.owner?.title.slice(0,200) || '',context:frame.owner ? context(frame.owner) : ''};
  }
  function targetInfo(target) {
    const {el,frame}=target;
    return {frameRef:frame.frameRef,role:role(el),name:name(el),tag:el.tagName.toLowerCase(),id:el.id.slice(0,200),context:context(el),disabled:disabled(el),editable:editable(el),readonly:Boolean(el.readOnly || el.getAttribute('aria-readonly')==='true'),...choiceState(el),...fileInfo(el)};
  }
  function choiceState(el) {
    if(el.tagName==='INPUT' && ['checkbox','radio'].includes(el.type))
      return el.type==='checkbox' ? {checked:el.checked,indeterminate:el.indeterminate} : {checked:el.checked};
    const kind=role(el);
    if(!['checkbox','radio'].includes(kind)) return {};
    const state=el.getAttribute('aria-checked');
    return {checked:state==='true' ? true : state==='false' ? false : kind==='checkbox' && state==='mixed' ? 'mixed' : null};
  }
  function formGroup(el) {
    for(let node=parent(el); node; node=parent(node)) {
      if(node.tagName==='FIELDSET' || ['radiogroup','group'].includes(role(node))) return node;
      if(node.tagName==='FORM') break;
    }
    return null;
  }
  function groupPrompt(el) {
    const root=el.getRootNode();
    const labelled=(el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id=>root.getElementById?.(id)).filter(Boolean);
    const chunks=[];
    let length=0;
    function collect(node) {
      if(length>2000) return;
      if(node.nodeType===3) {const text=node.textContent.replace(/\s+/g,' ').trim();if(text){chunks.push(text);length+=text.length+1;}return;}
      if(node.nodeType!==1 || !visible(node) || secret(node) || ['SCRIPT','STYLE','NOSCRIPT','TEMPLATE'].includes(node.tagName)) return;
      if(node!==el && (editable(node) || ['radio','checkbox','button','combobox'].includes(role(node)) ||
          (node.tagName==='LABEL' && (node.control || node.querySelector('input,select,textarea'))))) return;
      for(const child of node.childNodes) collect(child);
    }
    if(labelled.length) for(const source of labelled) collect(source);
    else if(el.getAttribute('aria-label')) chunks.push(el.getAttribute('aria-label'));
    // A short accessible group caption (e.g. Question 1) can coexist with a
    // longer visible prompt. Retain both, without repeating labels/options.
    collect(el);
    const text=[...new Set(chunks)].join(' ').trim();
    return {name:text.slice(0,2000),...(text.length>2000 ? {nameTruncated:true} : {})};
  }
  function snapshot(options={}) {
    const {scope='all',role_filter='',frame_ref='',offset=0,limit=150,include_text=true}=options;
    if(!['all','editable','form'].includes(scope) || typeof role_filter!=='string' || typeof frame_ref!=='string' || !Number.isInteger(offset) || offset<0 || !Number.isInteger(limit) || limit<1 || limit>150 || typeof include_text!=='boolean') throw new Error('Invalid snapshot scope/filter/offset/limit/include_text');
    const data=scan();
    if(frame_ref && !data.frames.some(f=>f.frameRef===frame_ref)) throw failure('stale_snapshot','Frame reference expired; request a fresh unscoped snapshot');
    const fields=new Set(['textbox','checkbox','radio','combobox','file']);
    const formFrames=new Set(data.elements.filter(t=>fields.has(role(t.el))).map(t=>t.frame));
    const selected=data.elements.filter(t=>(scope!=='editable' || editable(t.el)) &&
      (scope!=='form' || fields.has(role(t.el)) || (role(t.el)==='button' &&
        (formFrames.has(t.frame) || t.el.closest('[role="dialog"],dialog')) && !t.el.closest('nav,[role="navigation"],aside'))) &&
      (!role_filter || role(t.el)===role_filter) && (!frame_ref || t.frame.frameRef===frame_ref));
    // Editable targets remain discoverable even when editor toolbars fill the page.
    if(scope==='all') selected.sort((a,b)=>Number(editable(b.el))-Number(editable(a.el)));
    const snapshotId=session+':'+(++sequence);
    observer.disconnect();refs=new Map();
    for(const root of data.roots) observer.observe(root,{childList:true,subtree:true});
    lastOptions={scope,role_filter,frame_ref,offset,limit,include_text};
    const result={url:location.href.slice(0,4096),title:document.title.slice(0,1000),snapshotId,text:include_text?data.text:'',textIncluded:include_text,...(include_text ? {textTruncated:data.textTruncated} : {}),
      capabilities:{check:true,checkBatchLimit:20,checkViaClick:true,pageVersion:VERSION,formSnapshot:true,upload:typeof globalThis.__astraCreateFileUpload==='function' && typeof DataTransfer==='function'},frames:data.frames.map(frameInfo),scope,offset,totalMatches:selected.length,nextOffset:null,elements:[],limitations:data.limitations};
    const groups=new Map();
    if(scope==='form') result.groups=[];
    for(const target of selected.slice(offset,offset+limit)) {
      const ref=snapshotId+':'+result.elements.length;refs.set(ref,target);
      let info={ref,...targetInfo(target)};
      if(scope==='form') {
        // Keep actionable identity/state, without repeating default flags and
        // group prompts on every option. Group refs are readable like any ref.
        const fullName=name(target.el,1001);
        info={ref,frameRef:target.frame.frameRef,role:role(target.el),name:fullName.slice(0,1000),
          ...(fullName.length>1000 ? {nameTruncated:true} : {}),
          ...(target.el.id ? {id:target.el.id.slice(0,200)} : {}),...choiceState(target.el),...fileInfo(target.el),
          ...(disabled(target.el) ? {disabled:true} : {}),
          ...(target.el.readOnly || target.el.getAttribute('aria-readonly')==='true' ? {readonly:true} : {})};
        const group=formGroup(target.el);
        if(group) {
          if(!groups.has(group)) {
            const id=groups.size, groupRef=snapshotId+':g'+id;
            groups.set(group,id);refs.set(groupRef,{el:group,frame:target.frame});
            result.groups.push({id,ref:groupRef,frameRef:target.frame.frameRef,...groupPrompt(group)});
          }
          info.group=groups.get(group);
        } else if(context(target.el)) info.context=context(target.el);
      }
      if(editable(target.el) || target.el.tagName==='SELECT') {const text=value(target.el);info.value=text.slice(0,12000);info.valueTruncated=text.length>12000;}
      result.elements.push(info);
    }
    if(location.href.length>4096) result.limitations.push('URL truncated at 4096 characters.');
    const overBudget=()=>{const text=JSON.stringify(result);return new Blob([text]).size>60000 || (scope==='form' && text.length>10000);};
    const pruneGroups=()=>{if(result.groups){const used=new Set(result.elements.map(e=>e.group));
      result.groups=result.groups.filter(g=>{if(used.has(g.id)) return true;refs.delete(g.ref);return false;});}};
    const updatePagination=()=>{
      if(offset+result.elements.length<selected.length) {
        result.nextOffset=offset+result.elements.length;
        const hint='More matching elements available; request nextOffset or narrow the frame/scope.';
        if(!result.limitations.includes(hint)) result.limitations.push(hint);
      }
    };
    // Pagination fields count towards the budget too, including after trimming.
    updatePagination();
    if(overBudget()) {
      result.limitations.push(scope==='form' ? 'Form observation limited to 10K characters; use nextOffset or frame_ref.' : 'Snapshot truncated to fit the 64 KiB transport budget; use frame_ref or pagination.');
      while(result.text.length && overBudget()) {result.text=result.text.slice(0,Math.floor(result.text.length*0.5));result.textTruncated=true;}
      while(result.elements.length && overBudget()) {refs.delete(result.elements.pop().ref);pruneGroups();updatePagination();}
      // Frame metadata itself can exceed the transport budget on long URLs.
      if(overBudget()) for(const f of result.frames) f.url=f.url.slice(0,200);
    }
    return result;
  }
  function query(selector,frameRef) {
    const data=scan(), found=[];
    if(frameRef && !data.frames.some(f=>f.frameRef===frameRef)) throw failure('stale_snapshot','Frame reference expired');
    for(const root of data.roots) {
      const doc=root.nodeType===9 ? root : root.ownerDocument;
      const frame=data.frames.find(f=>f.doc===doc);
      if(frameRef && frame.frameRef!==frameRef) continue;
      for(const el of root.querySelectorAll(selector)) found.push({el,frame});
    }
    return found;
  }
  function signature() {
    const data=scan();
    return JSON.stringify([location.href,document.title,data.text,data.elements.map(({el})=>[role(el),name(el),disabled(el),secret(el)?null:el.value,el.checked,el.indeterminate,el.getAttribute('aria-checked'),el.getAttribute('aria-expanded'),el.getAttribute('aria-pressed')])]);
  }
  function afterSnapshot() {
    // A navigation may remove the previously selected frame; observe the whole page then.
    try{return snapshot(lastOptions);}catch{return snapshot({include_text:lastOptions.include_text ?? true});}
  }
  function nextTask() {
    // Timers in long-hidden Chromium tabs can run only once a minute. Yield a
    // task for event handlers/microtasks without an arbitrary page timer. This
    // observes the local edit, not completion of asynchronous application saves.
    if(typeof MessageChannel!=='function') return Promise.resolve();
    return new Promise(resolve=>{
      const {port1,port2}=new MessageChannel();
      port1.onmessage=()=>{port1.close();port2.close();resolve();};
      port2.postMessage(null);
    });
  }
  async function run(operation,args={}) {
    if(operation==='invalidate'){uploadEngine?.abort();generation++;observer.disconnect();refs.clear();lastOptions={};return {status:'observed',message:'References invalidated'};}
    let dispatched=false;
    try {
      if(args.expectedOrigin && location.origin!==args.expectedOrigin) throw new Error('Page origin changed; renewed approval required');
      if(operation==='snapshot') {uploadEngine?.abort();return snapshot(args);}
      if(operation.startsWith('upload_')) {
        if(typeof globalThis.__astraCreateFileUpload!=='function') return {status:'unsupported_operation',dispatch_state:'not_dispatched',message:'Reload the updated Browser Control extension'};
        uploadEngine ??= globalThis.__astraCreateFileUpload({resolve:args=>{
          const target=resolveChoice(args);
          if(disabled(target.el)) throw failure('error','File input is disabled');
          return {...target,generation,fileReadbackAnchor:fileReadbackAnchor(target.el)};
        },retained:t=>t.generation===generation && validTarget(t),
          clearedReplacement:clearedFileReplacement,
          describe:targetInfo,after:()=>snapshot({...lastOptions,include_text:false}),nextTask});
        return await uploadEngine.run(operation,args);
      }
      if(operation==='check') return await checkSelections(args);
      // Old protocol-1 controllers already authorize click as a write. Keep the
      // same goal validation/verification engine instead of replaying raw clicks.
      if(operation==='click' && Object.hasOwn(args,'choiceGoals')) return await checkSelections({checks:args.choiceGoals});
      if(!['click','type','fill','read','select','probe'].includes(operation)) throw new Error('Unsupported operation');
      const ref=args.ref || (typeof args.selector==='string' && args.selector.startsWith('ref:') ? args.selector.slice(4) : '');
      let target;
      if(ref) {
        forgetRemoved(observer.takeRecords());target=refs.get(ref);
        if(!target || !validTarget(target)) throw failure('stale_snapshot','Reference expired; request a fresh snapshot');
        if(args.frame_ref && target.frame.frameRef!==args.frame_ref) throw failure('stale_snapshot','Reference belongs to a different frame');
      } else if(operation!=='probe' || args.selector) {
        if(typeof args.selector!=='string' || !args.selector) throw new Error('A ref or CSS selector is required');
        const found=query(args.selector,args.frame_ref);
        if(operation==='probe' && !found.length) return {matched:false};
        if(found.length!==1) throw failure(found.length?'ambiguous_target':'target_not_found',found.length?'Ambiguous selector: multiple elements; use a frame-bound ref':'Element not found');
        target=found[0];
      }
      if(operation==='probe') {
        if(!ref && !args.selector && !args.text && !args.urlContains) throw new Error('A wait condition is required');
        return {matched:(!target || visible(target.el)) && (!args.text || scan().text.includes(args.text)) && (!args.urlContains || location.href.includes(args.urlContains))};
      }
      const {el,frame}=target, view=el.ownerDocument.defaultView;
      if(operation==='read' && fileInput(el)) return {status:'observed',target:targetInfo(target),...fileInfo(el)};
      if(!visible(el)) throw new Error('Target is not visible');
      for(let f=frame; f.owner; f=f.parent) if(!visible(f.owner)) throw new Error('Containing frame is not visible');
      if(secret(el)) throw new Error('Password, file and hidden inputs are not supported');
      if(operation==='read') {
        const text=value(el), offset=args.offset ?? 0;
        if(!Number.isInteger(offset) || offset<0) throw new Error('offset must be a non-negative integer');
        return {status:'observed',target:targetInfo(target),value:text.slice(offset,offset+12000),valueTruncated:text.length>offset+12000,
          valueOffset:offset,valueLength:text.length,...selectOptions(el,100)};
      }
      if(disabled(el)) throw new Error('Target is disabled');
      const filling=['type','fill'].includes(operation);
      if(filling) {
        if(typeof args.text!=='string' || args.text.length>12000) throw new Error('Text must be a string of at most 12000 characters');
        if(el.readOnly || el.getAttribute('aria-readonly')==='true') throw new Error('Target is readonly');
        if(!editable(el)) throw new Error('Target is not editable');
      }
      let option, matchedBy='value';
      if(operation==='select') {
        if(el.tagName!=='SELECT') throw new Error('Target is not a select');
        const all=[...el.options];
        option=all.find(x=>x.value===args.value);
        let labelled=0;
        if(!option && typeof args.value==='string') {
          // A visible label selects only when exactly one option carries it.
          const wanted=args.value.replace(/\s+/g,' ').trim(), matches=wanted ? all.filter(x=>optionLabel(x)===wanted) : [];
          labelled=matches.length;
          if(labelled===1) {option=matches[0];matchedBy='label';}
        }
        if(!option || !optionUsable(option)) return {status:'error',message:'Selectable option not found: '+
          (option ? 'that option is disabled' : labelled>1 ? 'several options share this label; use a value' : 'no option has this value or exact label')+
          '. Choose from options.',...selectOptions(el,100)};
      }
      const before=signature(), beforeValue=filling ? value(el) : '', identity=targetInfo(target);
      // No replay is safe after this boundary, including event-handler exceptions.
      dispatched=true;
      if(operation==='click') el.click();
      else if(filling && contentEditable(el)) {
        // Selection belongs to this exact Document. This does not activate the tab,
        // browser window or system clipboard, unlike OS keyboard/paste fallbacks.
        el.focus({preventScroll:true});
        if(el.getRootNode().activeElement!==el) throw new Error('Editor refused internal focus');
        const selection=el.ownerDocument.getSelection(), range=el.ownerDocument.createRange();
        range.selectNodeContents(el);selection.removeAllRanges();selection.addRange(range);
        if(typeof el.ownerDocument.execCommand!=='function') throw new Error('Native contenteditable insertion unavailable');
        // insertText turns newlines into separate paragraphs in TinyMCE. Encode
        // plain text as HTML with explicit line breaks, never accept raw markup.
        const text=args.text.replace(/\r\n?/g,'\n');
        const html=text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
        el.ownerDocument.execCommand(text ? 'insertHTML' : 'delete',false,html+(text.endsWith('\n')?'<br>':''));
        el.dispatchEvent(new view.Event('change',{bubbles:true,composed:true}));
        el.blur();
      } else {
        const proto=operation==='select'?view.HTMLSelectElement.prototype:el.tagName==='TEXTAREA'?view.HTMLTextAreaElement.prototype:view.HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto,'value').set.call(el,filling?args.text:option.value);
        // Options can share a value; keep the one that was matched.
        if(!filling && el.selectedIndex!==option.index) Object.getOwnPropertyDescriptor(proto,'selectedIndex').set.call(el,option.index);
        el.dispatchEvent(new view.Event('input',{bubbles:true,composed:true}));
        el.dispatchEvent(new view.Event('change',{bubbles:true,composed:true}));
      }
      await nextTask();
      if(filling) {
        forgetRemoved(observer.takeRecords());
        if(!validTarget(target) || (ref && refs.get(ref)!==target)) throw new Error('Target changed during fill');
        const actual=value(el), verified=actual===args.text.replace(/\r\n?/g,'\n');
        return {status:verified?(operation==='type'?'observed':'verified'):'verification_failed',verified,target:identity,
          beforeValue:beforeValue.slice(0,12000),value:actual.slice(0,12000),valueTruncated:actual.length>12000,
          message:verified?'Target value read back and matched; application save must be checked separately':'Target value did not match; inspect before deciding next action',after:afterSnapshot()};
      }
      const changed=signature()!==before;
      return {status:changed?'observed':'no_observed_change',message:changed?'Page change observed; task success is not implied':'Action dispatched once; no page change observed',
        ...(option ? {option:{value:option.value.slice(0,120),label:optionLabel(option).slice(0,120),matchedBy}} : {}),after:afterSnapshot()};
    } catch(error) {
      return {status:dispatched?'unknown_outcome':error.status || 'error',message:dispatched?'Action may have executed; inspect target before deciding next action; do not replay automatically':String(error.message || error)};
    }
  }
  function resolveChoice(args) {
    const ref=args.ref || (typeof args.selector==='string' && args.selector.startsWith('ref:') ? args.selector.slice(4) : '');
    let target;
    if(ref) {
      forgetRemoved(observer.takeRecords());target=refs.get(ref);
      if(!target || !validTarget(target)) throw failure('stale_snapshot','Reference expired; observe once and use fresh references');
      if(args.frame_ref && target.frame.frameRef!==args.frame_ref) throw failure('stale_snapshot','Reference belongs to another frame');
    } else {
      if(typeof args.selector!=='string' || !args.selector) throw failure('error','A ref or unique CSS selector is required');
      const found=query(args.selector,args.frame_ref);
      if(found.length!==1) throw failure(found.length?'ambiguous_target':'target_not_found','Choice selector must identify exactly one element');
      target=found[0];
    }
    return target;
  }
  function assertChoice(target, checked) {
    if(!validTarget(target)) throw failure('stale_snapshot','Choice document or element changed');
    const {el,frame}=target;
    if(!visible(el) || secret(el)) throw failure('error','Choice is not visible');
    for(let f=frame;f.owner;f=f.parent) if(!visible(f.owner)) throw failure('error','Containing frame is not visible');
    if(!['checkbox','radio'].includes(role(el)) || typeof choiceState(el).checked!=='boolean')
      throw failure('verification_failed','Choice has no reliable boolean checked state');
    if(role(el)==='radio' && !checked && choiceState(el).checked)
      throw failure('error','Select another radio in the group; an active radio cannot be unchecked by clicking');
    if(choiceState(el).checked!==checked && (disabled(el) || el.getAttribute('aria-readonly')==='true'))
      throw failure('error','Choice is disabled or readonly');
  }
  async function checkSelections(args) {
    const results=[], targets=[], boundGeneration=generation;
    let dispatched=0, currentDispatched=false, currentIndex=0;
    const receipt=(status,message)=>({status,message,verified:status==='verified',
      dispatch_state:dispatched?'dispatched':'not_dispatched',verification_state:status==='verified'?'satisfied':'not_verified',
      completed:results.length,failedIndex:status==='verified'?null:currentIndex,clickCount:dispatched,results});
    function bounded(result) {
      const after=result.after;
      const overBudget=()=>{const text=JSON.stringify(result);return new Blob([text]).size>60000 || (after?.scope==='form' && text.length>10000);};
      if(after) while(after.elements.length && overBudget()) {
        refs.delete(after.elements.pop().ref);
        after.nextOffset=after.offset+after.elements.length;
        if(!after.limitations.includes('Choice receipt reduced the observation; use nextOffset.'))
          after.limitations.push('Choice receipt reduced the observation; use nextOffset.');
      }
      return result;
    }
    try {
      const checks=args.checks ?? [{selector:args.selector,ref:args.ref,frame_ref:args.frame_ref,checked:args.checked}];
      if(!Array.isArray(checks) || checks.length<1 || checks.length>20) throw failure('error','Provide 1..20 choice goals');
      const seen=new Set(), radioGroups=[];
      for(const [index,item] of checks.entries()) {
        currentIndex=index;
        if(!item || typeof item.checked!=='boolean') throw failure('error','checked must be boolean');
        const target=resolveChoice(item);assertChoice(target,item.checked);
        if(seen.has(target.el)) throw failure('error','Duplicate choice target');
        seen.add(target.el);
        const el=target.el;
        if(el.tagName==='INPUT' && el.type==='radio' && el.name && item.checked) {
          if(radioGroups.some(other=>other.name===el.name && other.form===el.form && other.getRootNode()===el.getRootNode()))
            throw failure('error','Conflicting goals for the same radio group');
          radioGroups.push(el);
        }
        targets.push({target,checked:item.checked});
      }
      const batchDeadline=performance.now()+1500;
      for(const [index,{target,checked}] of targets.entries()) {
        currentIndex=index;currentDispatched=false;
        if(performance.now()>=batchDeadline) throw failure('timeout','Choice batch time budget reached; remaining goals were not dispatched');
        if(generation!==boundGeneration) throw failure('stale_snapshot','Control invalidated; batch stopped');
        assertChoice(target,checked);
        const before=choiceState(target.el).checked;
        if(before!==checked) {
          // At most one click per goal. Unknown results never restart the batch.
          currentDispatched=true;dispatched++;target.el.click();
          const deadline=Math.min(batchDeadline,performance.now()+400);
          do {
            if(typeof MessageChannel==='function') await nextTask();
            else await new Promise(resolve=>setTimeout(resolve,0));
            if(generation!==boundGeneration || !validTarget(target)) throw failure('unknown_outcome','Target changed after dispatch');
            if(choiceState(target.el).checked===checked) break;
          } while(performance.now()<deadline);
        }
        if(choiceState(target.el).checked!==checked) throw failure('verification_failed','Choice did not reach the requested state; no repeated click was sent');
        results.push({index,checked,before,clicked:currentDispatched,target:targetInfo(target)});
      }
      // Later radio actions or application handlers can undo earlier selections.
      for(const [index,{target,checked}] of targets.entries()) {
        currentIndex=index;currentDispatched=false;
        if(generation!==boundGeneration || !validTarget(target) || choiceState(target.el).checked!==checked)
          throw failure('verification_failed','A completed choice changed during the batch; inspect before continuing');
      }
      const result=receipt('verified','All target checked states matched; application save is separate');
      result.after=snapshot({...lastOptions,include_text:false});
      return bounded(result);
    } catch(error) {
      const status=error.status || (dispatched?'unknown_outcome':'error');
      const result=receipt(status,String(error.message || error));
      result.dispatch_state=dispatched ? (status==='unknown_outcome'?'unknown':'partial') : 'not_dispatched';
      if(generation===boundGeneration && targets.every(({target})=>validFrame(target.frame))) {
        try {result.after=snapshot({...lastOptions,include_text:false});} catch {}
      }
      return bounded(result);
    }
  }
  // Also protects the CDP backend and direct helper callers, not just the worker.
  let pending=Promise.resolve();
  globalThis.__astraBrowserPage=(operation,args={})=>{
    // Revocation takes effect immediately even while a write awaits verification.
    if(operation==='invalidate') return run(operation,args);
    const boundGeneration=generation;
    const result=pending.then(()=>boundGeneration===generation?run(operation,args):{status:'stale_snapshot',message:'Control invalidated while queued; observe again'});
    pending=result.catch(()=>{});return result;
  };
})();
