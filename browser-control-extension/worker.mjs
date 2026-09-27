import {createControl} from './control.mjs';
const RETRY_ALARM='astra-browser-reconnect';
export function installWorker(api,{retryDelays=[1000,3000,10000],handshakeTimeoutMs=10000}={}) {
  const control=createControl(api);
  let stoppedGeneration=0,permissionRevision=0;
  let port=null,autoConnect=false,saved=[],phase='stopped',epoch=0,retries=0,timer=null,handshake=null;
  let connectionError='';
  const errorText=value=>String(value || 'Native host disconnected. Start Astra and reconnect.').replace(/[\u0000-\u001f\u007f]/g,' ').slice(0,400);
  // Storage/lifecycle writes are ordered, but cancellation invalidates authority immediately.
  let tail=Promise.resolve();
  const serial=fn=>{const result=tail.then(fn);tail=result.catch(()=>{});return result;};
  const state=()=>({...control.state(),autoConnect,connected:phase==='ready'&&control.state().enabled,connectionState:phase==='ready'&&!control.state().enabled?'connecting':phase,connectionError});
  const persist=()=>api.storage.session.set({grants:autoConnect?saved:[]});
  function cancelTimers(){clearTimeout(timer);clearTimeout(handshake);timer=handshake=null;}
  function retry(){
    if(!autoConnect) return;
    phase='waiting';
    // Alarm is also a fallback if a worker is suspended during a fast timer.
    api.alarms.create(RETRY_ALARM,{delayInMinutes:0.5,periodInMinutes:0.5});
    if(retries<retryDelays.length) {timer=setTimeout(()=>{timer=null;connect();},retryDelays[retries++]);timer.unref?.();}
  }
  function disconnected(current){
    if(port!==current) return;
    port=null;epoch++;cancelTimers();
    if(autoConnect) {if(phase==='ready')saved=control.exportGrants();control.disconnect();}else control.stop();
    phase=autoConnect?'waiting':'stopped';
    serial(async()=>{if(!autoConnect)saved=[];await persist();}).catch(()=>{});
    retry();
  }
  function connect(){
    if(port) return;
    cancelTimers();phase=retries?'reconnecting':'connecting';
    const token=++epoch;
    let current;
    try {current=api.runtime.connectNative('com.astra.browser_control');port=current;}
    catch(error) {connectionError=errorText(error?.message);phase='stopped';retry();return;}
    let readyGate=null;
    current.onDisconnect.addListener(()=>{const error=api.runtime.lastError;if(port===current)connectionError=errorText(error?.message);disconnected(current);});
    current.onMessage.addListener(request=>{
      if(port!==current || epoch!==token) return;
      if(request?.type==='astra_control_ready' && request.version===1) {
        if(readyGate) return;
        clearTimeout(handshake);
        const permissionsAtReady=permissionRevision;
        readyGate=serial(async()=>{
          if(!await control.restore(autoConnect?saved:[]))throw new Error('Restoration cancelled');
          if(port!==current || epoch!==token) return;
          saved=control.exportGrants();await persist();
          if(port!==current || epoch!==token) return;
          if(permissionsAtReady!==permissionRevision)throw new Error('Permissions changed during restoration');
          control.enable();
          // Additive announcement: old runtimes ignore this unknown response ID.
          // Send it first so new runtimes know capabilities before becoming ready.
          current.postMessage({id:'astra_control_capabilities',ok:true,result:control.capabilities()});
          current.postMessage({id:'astra_control_ready',ok:true,result:{version:1}});
          phase='ready';retries=0;connectionError='';
          await api.alarms.clear(RETRY_ALARM);
        });
        readyGate.catch(()=>{if(port===current){disconnected(current);current.disconnect();}});
        return;
      }
      if(!readyGate) return;
      // This gate belongs only to this authenticated connection. Nothing is replayed.
      readyGate.then(async()=>{
        if(port!==current || epoch!==token || phase!=='ready') return;
        const response=await control.handle(request);
        await serial(async()=>{
          if(port!==current || epoch!==token) return;
          saved=control.exportGrants();await persist();
          if(port===current && epoch===token) current.postMessage(response);
        });
      }).catch(()=>{if(port===current){disconnected(current);current.disconnect();}});
    });
    handshake=setTimeout(()=>{if(port===current){connectionError='Native host handshake timed out. Start Astra and check astra browser-control status.';disconnected(current);current.disconnect();}},handshakeTimeoutMs);handshake.unref?.();
  }
  function stop(){
    stoppedGeneration++;autoConnect=false;epoch++;cancelTimers();const old=port;port=null;
    control.stop();phase='stopped';connectionError='';old?.disconnect();
    return serial(async()=>{autoConnect=false;saved=[];await api.storage.local.set({autoConnect:false});await persist();await api.alarms.clear(RETRY_ALARM);});
  }
  const initialGeneration=stoppedGeneration;
  const initialized=serial(async()=>{
    const [local,session]=await Promise.all([api.storage.local.get('autoConnect'),api.storage.session.get('grants')]);
    if(stoppedGeneration!==initialGeneration)return;
    autoConnect=local.autoConnect===true;saved=autoConnect&&Array.isArray(session.grants)?session.grants:[];
    if(autoConnect){await api.alarms.create(RETRY_ALARM,{delayInMinutes:0.5,periodInMinutes:0.5});if(stoppedGeneration===initialGeneration)connect();}else await persist();
  });
  initialized.catch(()=>{phase='stopped';});
  api.runtime.onMessage.addListener((message,sender,respond)=>{
    if(sender.id!==api.runtime.id || sender.tab || sender.url!==api.runtime.getURL('popup.html')) {respond({ok:false,error:'Popup only'});return false;}
    (async()=>{
      if(message?.operation==='stop'){await stop();return state();}
      const generation=stoppedGeneration;
      await initialized;
      if(generation!==stoppedGeneration)return state();
      switch(message?.operation) {
        case 'connect':connect();return state();
        case 'autoConnect':return await serial(async()=>{if(generation!==stoppedGeneration)return state();autoConnect=message.enabled===true;await api.storage.local.set({autoConnect});saved=control.exportGrants();await persist();if(autoConnect)connect();else {clearTimeout(timer);timer=null;await api.alarms.clear(RETRY_ALARM);}return state();});
        case 'grant':return await serial(async()=>{const result=await control.grant(message.tabId);saved=control.exportGrants();await persist();return result;});
        case 'state':return state();
        default:throw new Error('Unsupported popup operation');
      }
    })().then(result=>respond({ok:true,result}),error=>respond({ok:false,error:String(error.message)}));
    return true;
  });
  api.runtime.onStartup.addListener(()=>{initialized.then(()=>{if(autoConnect)connect();}).catch(()=>{});});
  api.alarms.onAlarm.addListener(alarm=>{if(alarm.name===RETRY_ALARM)initialized.then(()=>{if(autoConnect)connect();});});
  api.tabs.onUpdated.addListener((id,change)=>{
    if(!change.url)return;
    control.navigation(id,change.url);
    serial(async()=>{saved=saved.filter(g=>{try{return g.tabId!==id || new URL(change.url).origin===g.origin;}catch{return g.tabId!==id;}});await persist();}).catch(()=>{});
  });
  api.tabs.onRemoved.addListener(id=>{control.revoke(id);serial(async()=>{saved=saved.filter(g=>g.tabId!==id);await persist();}).catch(()=>{});});
  api.permissions.onRemoved.addListener(()=>{
    permissionRevision++;
    // Invalidate in-flight grants immediately, including permission checks.
    control.disconnect();
    const generation=stoppedGeneration, connectionEpoch=epoch, permissionsAtRemoval=permissionRevision;
    serial(async()=>{
      if(generation!==stoppedGeneration || connectionEpoch!==epoch || permissionsAtRemoval!==permissionRevision)return;
      if(!await control.restore(saved))return;
      if(generation!==stoppedGeneration || connectionEpoch!==epoch || permissionsAtRemoval!==permissionRevision)return;
      saved=control.exportGrants();await persist();
      if(generation===stoppedGeneration && connectionEpoch===epoch && permissionsAtRemoval===permissionRevision && phase==='ready')control.enable();
    }).catch(()=>{});
  });
  return {control};
}
if(typeof chrome!=='undefined' && chrome.runtime?.id) installWorker(chrome);
