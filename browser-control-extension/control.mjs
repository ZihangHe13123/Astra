const MAX_BYTES = 1024 * 1024;
const OPERATIONS = new Set(['upload_prepare','upload_chunk','upload_commit','upload_abort','tabs','attach','open','snapshot','click','type','fill','check','read','select','wait','screenshot','handoff','resume','close']);
const bytes = value => new TextEncoder().encode(JSON.stringify(value)).length;
// Errors are capped at 500 characters; an address too long to quote whole is given as its origin.
const shown = url => url.length<=300 ? url : new URL(url).origin;
function origin(tab) {
  if (!tab || tab.incognito || !Number.isInteger(tab.id)) throw new Error('Private or missing tab');
  const url = new URL(tab.url);
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Only HTTP(S) tabs are supported');
  return url.origin;
}
export function createControl(api, {openTimeoutMs=10000}={}) {
  let enabled = false, epoch = 0, grantSequence = 0;
  const grants = new Map(), seen = new Set(), revisions = new Map(), queues = new Map(), uploads = new Map(), moved = new Map();
  const revision=id=>revisions.get(id)||0;
  const changed=id=>revisions.set(id,revision(id)+1);
  const session = crypto.randomUUID();
  const check = token => { if (!enabled || token !== epoch) throw new Error('Control stopped or disconnected'); };
  async function openedDocument(id, expected, token) {
    const deadline=performance.now()+openTimeoutMs;
    while(true) {
      check(token);
      const tab=await api.tabs.get(id);
      check(token);
      if(tab.incognito || tab.id!==id) throw new Error('Private or missing tab');
      // tabs.create resolves before the initial navigation commits. Do not
      // expose a grant or inject into that transient empty/about:blank page.
      const pending=!tab.url || tab.url==='about:blank';
      if(!pending && origin(tab)!==expected) throw new Error(`Origin changed while opening: the page went to ${shown(tab.url)} and this tab was closed. Open that address instead`);
      if(!pending && tab.status==='complete') return tab;
      if(performance.now()>=deadline) throw new Error(pending
        ? 'New tab navigation timed out before the page address could be read (a slow page, or a redirect to a site without website permission); no page control was granted'
        : 'New tab navigation timed out; no page control was granted');
      await new Promise(resolve=>setTimeout(resolve,Math.min(50,Math.max(0,deadline-performance.now()))));
    }
  }
  async function target(id, token) {
    check(token);
    if (!Number.isInteger(id) || !grants.has(id)) throw new Error(moved.get(id) || 'Tab requires explicit popup grant');
    const grant = grants.get(id), tab = await api.tabs.get(id);
    check(token);
    let current;
    try { current = origin(tab); } catch (error) { revoke(id); throw error; }
    if (current !== grant.origin) throw left(id, grant, tab.url);
    if (grants.get(id) !== grant) throw new Error('Tab grant revoked');
    return {tab,grant};
  }
  function approvedOrigin(args, grant, required=false) {
    const expected=args.expectedOrigin;
    if(expected===undefined) {
      if(required) throw new Error('An approved expectedOrigin is required');
      return grant.origin;
    }
    if(typeof expected!=='string' || new URL(expected).origin!==expected || expected!==grant.origin)
      throw new Error('Approved origin differs from the current tab grant');
    return expected;
  }
  function page(id, operation, args, token) {
    const grant=grants.get(id), grantToken=grant?.token;
    const result=(queues.get(id) || Promise.resolve()).then(()=>{
      check(token);
      if(grants.get(id)!==grant || grant?.token!==grantToken) throw new Error('Tab grant changed while queued; request a fresh snapshot');
      return pageNow(id,operation,args,token);
    });
    const settled=result.catch(()=>{});
    queues.set(id,settled);
    void settled.then(()=>{if(queues.get(id)===settled) queues.delete(id);});
    return result;
  }
  async function pageNow(id, operation, args, token) {
    const {grant} = await target(id, token);
    const write=['upload_commit','click','type','fill','check','select'].includes(operation);
    const expected=approvedOrigin(args,grant,write || operation.startsWith('upload_')), grantToken=grant.token;
    if (grant.paused) throw new Error('Tab handed off; resume explicitly');
    if(operation==='upload_prepare') uploads.set(id,grantToken);
    await api.scripting.executeScript({target:{tabId:id},world:'ISOLATED',files:['file-upload.js','page.js']});
    const current=await target(id, token);
    if(current.grant!==grant || grant.token!==grantToken) throw new Error('Tab grant changed before dispatch');
    approvedOrigin(args,grant,write || operation.startsWith('upload_'));
    if (grant.paused) throw new Error('Tab handed off before dispatch');
    try {
      // Once executeScript is submitted, rejection cannot prove no write occurred.
      const results = await api.scripting.executeScript({target:{tabId:id},world:'ISOLATED',func:async (op, input, expected, grantToken) => {
        if (location.origin !== expected) return {status:'error',message:'Origin changed before dispatch'};
        if (globalThis.__astraBrowserGrantToken !== grantToken) {
          await globalThis.__astraBrowserPage('invalidate', {});
          globalThis.__astraBrowserGrantToken = grantToken;
        }
        if (location.origin !== expected) return {status:'error',message:'Origin changed before dispatch'};
        return globalThis.__astraBrowserPage(op, {...input, expectedOrigin:expected});
      },args:[operation,args,expected,grantToken]});
      check(token);
      if (!results?.[0] || results[0].result === undefined) throw new Error('No page result');
      if(['upload_commit','upload_abort'].includes(operation)) uploads.delete(id);
      return results[0].result;
    } catch(error) {
      if(write) return {status:'unknown_outcome',dispatch_state:'unknown',repeat_input:false,
        message:`${error.message || error}; write was submitted and will not be replayed`};
      throw error;
    }
  }
  async function waitFor(id,args,token) {
    const ms=args.timeoutMs ?? 500;
    if(!Number.isFinite(ms)||ms<0||ms>10000) throw new Error('wait timeoutMs must be 0..10000');
    const conditional=Boolean(args.ref || args.selector || args.text || args.urlContains);
    if(!conditional) {
      await new Promise(resolve=>setTimeout(resolve,ms));
      return {status:'observed',after:await page(id,'snapshot',args,token)};
    }
    const deadline=performance.now()+ms;
    while(true) {
      const probe=await page(id,'probe',args,token);
      if(probe.status) return probe;
      if(probe.matched===true) return {status:'observed',after:await page(id,'snapshot',args,token)};
      if(performance.now()>=deadline) return {status:'timeout',message:'Wait conditions were not observed before timeout',after:await page(id,'snapshot',args,token)};
      await new Promise(resolve=>setTimeout(resolve,Math.min(100,Math.max(0,deadline-performance.now()))));
    }
  }
  function discardUpload(id) {
    const grantToken=uploads.get(id);
    if(!grantToken) return;
    uploads.delete(id);
    void api.scripting.executeScript({target:{tabId:id},world:'ISOLATED',func:token=>{
      if(globalThis.__astraBrowserGrantToken===token) return globalThis.__astraBrowserPage?.('invalidate',{});
    },args:[grantToken]}).catch(()=>{});
  }
  function revoke(id) { discardUpload(id);changed(id); grants.delete(id);moved.delete(id); }
  // The grant ends either way. Only a tab the agent opened says where it went, so later
  // requests can name the address; a user's own tab is not described after it leaves its grant.
  function left(id, grant, url) {
    const error=new Error(grant.owned
      ? `Origin changed: this tab went to ${shown(url)} and is no longer controlled. Open that address as a new tab`
      : 'Origin changed; this tab is no longer granted. The user can grant it again in the extension popup');
    revoke(id);
    if(grant.owned) {moved.set(id,error.message);if(moved.size>50) moved.delete(moved.keys().next().value);}
    return error;
  }
  function stop() { for(const id of grants.keys()) discardUpload(id);enabled=false;epoch++;grants.clear();moved.clear(); /* IDs survive reconnect: never replay. */ }
  return {
    enable(){enabled=true;},stop,revoke,
    capabilities(){return {version:1,controllerVersion:3,extensionVersion:api.runtime?.getManifest?.().version || '',
      operations:[...OPERATIONS].filter(op=>op!=='screenshot'),waitTimeoutMs:10000};},
    disconnect(){for(const id of grants.keys()) discardUpload(id);enabled=false;epoch++;},
    exportGrants(){return [...grants].map(([tabId,g])=>({tabId,origin:g.origin,owned:g.owned,paused:g.paused}));},
    async restore(saved) {
      const token=epoch, restored=[];
      grants.clear();
      for(const [g,version] of saved.map(g=>[g,revision(g.tabId)])) {
        try {
          if(!Number.isInteger(g.tabId) || typeof g.owned!=='boolean' || typeof g.paused!=='boolean') continue;
          const tab=await api.tabs.get(g.tabId);
          if(origin(tab)!==g.origin || !await api.permissions.contains({origins:[g.origin+'/*']})) continue;
          restored.push([g.tabId,version,{origin:g.origin,owned:g.owned,paused:g.paused,token:`${session}:${++grantSequence}`}]);
        } catch {}
      }
      if(token!==epoch) return false;
      for(const [id,version,g] of restored) if(revision(id)===version) grants.set(id,g);
      return true;
    },
    navigation(id,url){ discardUpload(id);changed(id); const grant=grants.get(id);if(grant){try{if(new URL(url).origin!==grant.origin) left(id,grant,url);else grant.token=`${session}:${++grantSequence}`;}catch{revoke(id);}} },
    state(){return {enabled,grantedTabIds:[...grants.keys()]};},
    async grant(id) {
      discardUpload(id);const token=epoch, version=revision(id);check(token);const tab=await api.tabs.get(id);check(token);
      const bound=origin(tab);
      if (!await api.permissions.contains({origins:[bound+'/*']})) throw new Error('Page permission not granted');
      check(token);if(revision(id)!==version)throw new Error('Tab changed while granting');grants.set(id,{origin:bound,owned:grants.get(id)?.owned || false,paused:false,token:`${session}:${++grantSequence}`});moved.delete(id);
      return {tabId:id,url:tab.url,title:tab.title || ''};
    },
    async handle(request) {
      const id = request?.id;
      try {
        if (typeof id !== 'string' || !id.length || id.length>128) throw new Error('Invalid request ID');
        if (bytes(request)>MAX_BYTES) throw new Error('Request exceeds 1MiB');
        if (seen.has(id)) throw new Error('Duplicate request; never replay');
        if (seen.size>=100000) throw new Error('Request ID capacity reached; restart extension');
        seen.add(id);
        const token=epoch;check(token);
        const {operation,args={}}=request;
        if (!OPERATIONS.has(operation)) throw Object.assign(new Error('Unsupported operation'),{code:'unsupported_operation',operation});
        if (!args || Array.isArray(args) || typeof args!=='object') throw Object.assign(new Error('Arguments must be an object'),{code:'invalid_arguments',operation});
        let tabId=request.tabId,result;
        if (operation==='tabs') {
          const tabs=[];
          for (const tid of [...grants.keys()]) {
            try {const {tab,grant}=await target(tid,token);tabs.push({id:tid,url:tab.url,title:tab.title||'',owned:grant.owned});}catch {if(grants.has(tid)) revoke(tid);}
          }
          check(token);result={tabs};
        } else if(operation==='open') {
          const url=new URL(args.url);if (!['http:','https:'].includes(url.protocol)) throw new Error('Only HTTP(S) URLs');
          if (!await api.permissions.contains({origins:[url.origin+'/*']})) throw new Error('Grant website permission in popup first');
          check(token);const tab=await api.tabs.create({url:url.href});
          try {
            const ready=await openedDocument(tab.id,url.origin,token);
            grants.set(tab.id,{origin:url.origin,owned:true,paused:false,token:`${session}:${++grantSequence}`});
            result={tabId:tab.id,url:ready.url,title:ready.title||''};
          } catch(error) {
            grants.delete(tab.id);
            await api.tabs.remove(tab.id).catch(()=>{});
            throw error;
          }
        } else {
          if(operation==='attach' && tabId===undefined) { if(grants.size!==1) throw new Error('Select an explicit granted tab ID');tabId=[...grants.keys()][0]; }
          const {tab,grant}=await target(tabId,token);
          approvedOrigin(args,grant,['click','type','select','handoff','resume'].includes(operation));
          if(operation==='snapshot' && args.metadataOnly===true) result={url:tab.url,title:tab.title||''};
          else if(operation==='attach') result={tabId,url:tab.url,title:tab.title||''};
          else if(operation==='handoff' || operation==='resume') {discardUpload(tabId);grant.token=`${session}:${++grantSequence}`;grant.paused=operation==='handoff';result={status:grant.paused?'handed_off':'resumed',tabId};}
          else if(operation==='close') {
            revoke(tabId);
            if(grant.owned) await api.tabs.remove(tabId);
            else await api.scripting.executeScript({target:{tabId},world:'ISOLATED',func:()=>globalThis.__astraBrowserPage?.('invalidate',{})}).catch(()=>{});
            result={status:grant.owned?'closed':'detached',tabId};
          } else if(operation==='screenshot') {
            throw new Error('unsupported: extension screenshot cannot atomically bind capture to a tab; use CDP');
          } else if(operation==='wait') {
            result=await waitFor(tabId,args,token);
          } else result=await page(tabId,operation,args,token);
        }
        const response={id,ok:true,result};if(bytes(response)>MAX_BYTES) throw new Error('Result exceeds 1MiB; narrow the request');return response;
      } catch(error) {return {id:typeof id==='string'?id.slice(0,128):null,ok:false,error:String(error.message || error).slice(0,500),
        ...(error.code ? {code:error.code,operation:error.operation,dispatch_state:'not_dispatched'} : {})};}
    }
  };
}
