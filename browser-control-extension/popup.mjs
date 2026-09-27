const status=document.querySelector('#status');
async function send(operation,extra={}) {
  const response=await chrome.runtime.sendMessage({operation,...extra});
  if(!response?.ok) throw new Error(response?.error || 'No worker response');
  return response.result;
}
async function refresh() {
  const state=await send('state');
  document.querySelector('#autoConnect').checked=state.autoConnect;
  document.querySelector('#grant').disabled=!state.connected;
  const labels={waiting:'Waiting for Astra. Automatic retry enabled.',connecting:'Connecting to Astra…',reconnecting:'Reconnecting to Astra…',stopped:'Stopped. Click Connect after starting Astra.'};
  status.textContent=state.connected ? `Connected. Granted tabs: ${state.grantedTabIds.join(', ') || 'none'}` : labels[state.connectionState];
  if(!state.connected && state.connectionError)status.textContent+=` ${state.connectionError}`;
}
function bind(id,action){document.querySelector(id).addEventListener('click',()=>action().catch(error=>{status.textContent=error.message;}));}
bind('#connect',async()=>{await send('connect');await refresh();});
bind('#grant',async()=>{
  const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
  if(!tab || tab.incognito) throw new Error('Select a regular HTTP(S) tab');
  const url=new URL(tab.url);if(!['http:','https:'].includes(url.protocol)) throw new Error('Only HTTP(S) tabs are supported');
  const allowed=await chrome.permissions.request({origins:[url.origin+'/*']});
  if(!allowed) throw new Error('Website permission declined');
  await send('grant',{tabId:tab.id});await refresh();
});
bind('#websites',async()=>{
  const allowed=await chrome.permissions.request({origins:['http://*/*','https://*/*']});
  status.textContent=allowed?'HTTP(S) permission granted. Only explicitly allowed or agent-created tabs can be controlled.':'Website permission declined';
});
bind('#stop',async()=>{await send('stop');await refresh();});
refresh().catch(error=>{status.textContent=error.message;});

document.querySelector('#autoConnect').addEventListener('change',async event=>{try{await send('autoConnect',{enabled:event.target.checked});await refresh();}catch(error){status.textContent=error.message;}});
setInterval(()=>refresh().catch(()=>{}),1000);
