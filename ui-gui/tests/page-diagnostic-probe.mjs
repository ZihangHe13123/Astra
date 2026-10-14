/** Fixed, isolated page/capture matrix. No model, user config, or production timeout overrides. */
import { build } from 'esbuild';
import { _electron as electron } from 'playwright';
import { launchElectron } from './electron-launch.mjs';
import electronPath from 'electron';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createServer } from 'node:http';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const folder = mkdtempSync(join(tmpdir(), 'astra-page-probe-'));
const output = join(root, 'output/playwright/page-diagnostic-probe'); mkdirSync(output, { recursive: true });
const selected = process.argv.find(arg => arg.startsWith('--driver='))?.slice(9) || 'both';
if (!['both', 'direct', 'playwright', 'playwright-raw'].includes(selected)) throw new Error('Use --driver=both|direct|playwright|playwright-raw');
const runFile = promisify(execFile);
const terminateOwned = async child => {
 if (!child || child.exitCode !== null || child.signalCode !== null) return;
 if (process.platform === 'win32') { await runFile('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { timeout: 5000, windowsHide: true }); return; }
 const { stdout } = await runFile('ps', ['-axo', 'pid=,ppid='], { timeout: 3000 });
 const rows = stdout.trim().split('\n').map(line => line.trim().split(/\s+/).map(Number));
 const owned = new Set([child.pid]);
 for (let changed = true; changed;) { changed = false; for (const [id, parent] of rows) if (owned.has(parent) && !owned.has(id)) { owned.add(id); changed = true; } }
 for (const id of [...owned].reverse()) { try { process.kill(id, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; } }
};
console.log(JSON.stringify({ event: 'probe-start', root, folder, selected }));
const drivers = selected === 'both' ? ['direct', 'playwright'] : [selected];
const rounds = 6;
const requests = [];
const server = createServer((req, res) => { requests.push(req.url); res.end('unexpected external navigation'); });
const port = await new Promise(done => server.listen(0, '127.0.0.1', () => done(server.address().port)));
const renderer = `import React from 'react'; import {createRoot} from 'react-dom/client';
import {PageFrame} from ${JSON.stringify(join(root, 'ui-gui/src/renderer/page-frame.tsx'))};
window.probeMessages=[];
addEventListener('message',event=>{if(event.data?.astraPage===true){window.probeMessages.push({at:Date.now(),type:event.data.type,id:event.data.id,sourceMatches:event.source===document.querySelector('iframe.page-frame')?.contentWindow});if(window.probeMessages.length>64)window.probeMessages.shift();}});
const root=createRoot(document.getElementById('root'));
window.probeShow=source=>new Promise(resolve=>{root.render(React.createElement(PageFrame,{source}));requestAnimationFrame(()=>requestAnimationFrame(resolve));});`;
const rendererFile = join(folder, 'renderer.js');
await build({ stdin: { contents: renderer, resolveDir: join(root, 'ui-gui'), loader: 'tsx' }, bundle: true, platform: 'browser', format: 'iife', outfile: rendererFile, define: { 'process.env.NODE_ENV': '"production"' } });
const preloadFile = join(folder, 'preload.cjs');
await build({ entryPoints: [join(root, 'ui-gui/src/preload/index.ts')], bundle: true, platform: 'node', format: 'cjs', external: ['electron'], outfile: preloadFile });
const entry = `
import {app,BrowserWindow,protocol} from 'electron';
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {join} from 'node:path';
import {PageSnapshots} from ${JSON.stringify(join(root, 'ui-gui/src/main/page-snapshot.ts'))};
import {cardFrameResponse,captureHostResponse,guardCardFrames} from ${JSON.stringify(join(root, 'ui-gui/src/main/card-frame.ts'))};
const base=process.env.ASTRA_PAGE_PROBE_DIR;
mkdirSync(base,{recursive:true});app.setName('Astra isolated page probe');app.setPath('userData',join(base,'user-data'));
protocol.registerSchemesAsPrivileged([{scheme:'astra',privileges:{standard:true,secure:true,supportFetchAPI:true}}]);
const events=[],results=[];let host;
const record=data=>{events.push({at:Date.now(),...data});if(events.length>800)events.shift();};
const save=()=>writeFileSync(join(base,'result.json'),JSON.stringify({mainPid:process.pid,versions:process.versions,driver:process.env.ASTRA_PAGE_PROBE_DRIVER,effectiveDisabledFeatures:app.commandLine.getSwitchValue('disable-features'),events,results},null,2));
app.on('web-contents-created',(_event,wc)=>{
 record({event:'contents-created',id:wc.id});
 for(const event of ['did-start-loading','did-stop-loading','destroyed','unresponsive'])wc.on(event,()=>record({event,id:wc.id}));
 wc.on('did-fail-load',(_event,code,_description,url,isMainFrame)=>record({event:'did-fail-load',id:wc.id,code,url:url.split('?')[0],isMainFrame}));
 wc.on('did-frame-navigate',(_event,url,_code,_status,isMainFrame)=>record({event:'did-frame-navigate',id:wc.id,url:url.split('?')[0],isMainFrame}));
 wc.on('render-process-gone',(_event,details)=>{record({event:'render-process-gone',id:wc.id,...details});save();});
});
app.on('child-process-gone',(_event,details)=>{record({event:'child-process-gone',...details});save();});
app.on('before-quit',()=>{record({event:'before-quit'});save();});
app.on('window-all-closed',()=>{});
process.on('uncaughtExceptionMonitor',error=>{record({event:'uncaughtException',name:error.name,message:error.message});save();});
const hostPage='<!doctype html><html><head><meta charset="utf-8"><style>:root{--bg:#fff;--text:#222;--accent:#246;color-scheme:light}iframe{width:850px;height:500px;border:0}</style></head><body><div id="root"></div><script src="astra://probe/renderer.js"></script></body></html>';
globalThis.pageProbeReady=(async()=>{
await app.whenReady();
protocol.handle('astra',request=>{const url=new URL(request.url);if(url.hostname==='card')return cardFrameResponse(url.pathname);if(url.hostname==='capture')return captureHostResponse(url.pathname);if(url.hostname==='probe')return new Response(url.pathname==='/renderer.js'?readFileSync(${JSON.stringify(rendererFile)}):hostPage,{headers:{'Content-Type':url.pathname==='/renderer.js'?'text/javascript':'text/html','Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; frame-src astra://card;"}});return new Response('not found',{status:404});});
host=new BrowserWindow({show:true,width:900,height:700,title:'Astra isolated page probe',webPreferences:{preload:${JSON.stringify(preloadFile)},contextIsolation:true,sandbox:true,nodeIntegration:false,nodeIntegrationInSubFrames:true}});
host.webContents.setWindowOpenHandler(()=>({action:'deny'}));host.webContents.on('will-navigate',event=>event.preventDefault());guardCardFrames(host.webContents);
await host.loadURL('astra://probe/index.html');
const snapshots=new PageSnapshots(join(base,'snapshots'),${JSON.stringify(preloadFile)},async()=>({dark:false,colors:{'--bg':'#fff','--text':'#222'}}));
let started=false;
globalThis.runPageProbe=async()=>{
 if(started)throw new Error('Probe is single-run');started=true;
 for(let round=0;round<${rounds};round++)for(const kind of ['normal','leaver']){
  const name=kind+'-'+round, path=join(base,name+'.page.html');
  const source=kind==='normal'?'<!doctype html><h1>PROBE '+round+'</h1>':'<!doctype html><p>leaving '+round+'</p><script>location.href="http://127.0.0.1:${port}/leaver-'+round+'"</'+'script>';
  writeFileSync(path,source);record({event:'case-start',name});save();
  const before=Date.now();
  await host.webContents.executeJavaScript('probeShow('+JSON.stringify(source)+')');
  let outcome;
  try{const image=await snapshots.capture(path);outcome={kind:'image',width:image.width,height:image.height};}
  catch(error){outcome={kind:'error',name:error.name,message:error.message};}
  const hostState=await host.webContents.executeJavaScript('({status:document.querySelector("[role=status]")?.textContent||"",frames:document.querySelectorAll("iframe.page-frame").length,messages:window.probeMessages})');
  const result={name,elapsedMs:Date.now()-before,outcome,hostState,windowCount:BrowserWindow.getAllWindows().length};
  results.push(result);record({event:'case-end',name,outcome:outcome.kind});save();
  if(kind==='normal'&&outcome.kind!=='image')throw new Error('Normal page did not produce an image');
  if(kind==='leaver'&&outcome.kind!=='error')throw new Error('Leaving page unexpectedly produced an image');
  if(result.windowCount!==1)throw new Error('Capture window remained after settlement');
 }
 record({event:'matrix-complete'});save();return {cases:results.length,mainPid:process.pid};
};
if(process.env.ASTRA_PAGE_PROBE_DRIVER==='direct'){
 try{await globalThis.runPageProbe();app.exit(0);}catch(error){record({event:'probe-error',name:error.name,message:error.message});save();app.exit(1);}
}
})().catch(error=>{record({event:'initialization-error',name:error.name,message:error.message});save();app.exit(1);throw error;});
`;
const mainFile = join(folder, 'main.cjs');
await build({ stdin: { contents: entry, resolveDir: join(root, 'ui-gui'), loader: 'ts' }, bundle: true, platform: 'node', format: 'cjs', external: ['electron', '@deepseek-ai/libreoffice-kit'], outfile: mainFile });
const bounded = (promise, ms, label) => { let timer; return Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error(label+' timed out')),ms);})]).finally(()=>clearTimeout(timer)); };
const summaries=[];
try {
 for(const driver of drivers){
  const dir=join(folder,driver);mkdirSync(dir);let stderr='',child,app;
  const lifecycle=[];const remember=(event,data={})=>lifecycle.push({at:Date.now(),event,...data});
  const env={...process.env,ASTRA_PAGE_PROBE_DIR:dir,ASTRA_PAGE_PROBE_DRIVER:driver};delete env.ELECTRON_RUN_AS_NODE;
  let failure;
  try{
   if(driver==='direct'){
    child=spawn(electronPath,[mainFile],{env,stdio:['ignore','ignore','pipe'],windowsHide:true});
   }else{
    const launch = driver === 'playwright-raw' ? options => electron.launch(options) : launchElectron;
    app=await launch({args:[mainFile],env,timeout:30000});child=app.process();
    app.on('close',()=>remember('app-close'));app.context().on('close',()=>remember('context-close'));
   }
   child.stderr?.on('data',chunk=>{stderr=(stderr+chunk.toString()).slice(-65536);});
   const exited=new Promise((resolve,reject)=>{child.on('error',reject);child.on('exit',(code,signal)=>{remember('launcher-exit',{code,signal});resolve({code,signal});});});
   if(driver==='direct'){const exit=await bounded(exited,150000,'Direct matrix');if(exit.code!==0)throw new Error('Direct Electron exit '+JSON.stringify(exit));}
   else{const value=await bounded(app.evaluate(async()=>{await globalThis.pageProbeReady;return globalThis.runPageProbe();}),150000,'Playwright matrix');remember('matrix-return',value);}
  }catch(error){failure={name:error.name,message:error.message};}
  finally{
   if(app){try{await bounded(app.close(),10000,'Playwright close');}catch(error){failure||={name:error.name,message:error.message};await terminateOwned(child).catch(error=>remember('cleanup-error',{message:error.message}));}}
   else if(child&&child.exitCode===null){await terminateOwned(child).catch(error=>remember('cleanup-error',{message:error.message}));}
  }
  let result;try{result=JSON.parse(readFileSync(join(dir,'result.json'),'utf8'));}catch{}
  const summary={driver,folder:dir,failure,lifecycle,launcher:{pid:child?.pid,exitCode:child?.exitCode,signalCode:child?.signalCode},result};summaries.push(summary);
  writeFileSync(join(output,driver+'-result.json'),JSON.stringify(summary,null,2));writeFileSync(join(output,driver+'-stderr.log'),stderr);
  console.log(JSON.stringify({driver,cases:result?.results.length,failure,folder:dir}));
 }
}finally{server.closeAllConnections();await new Promise(done=>server.close(done));}
writeFileSync(join(output,'summary.json'),JSON.stringify({root,folder,requests,summaries},null,2));
if(requests.length||summaries.some(item=>item.failure))process.exitCode=1;
console.log(JSON.stringify({output,requests:requests.length,success:!process.exitCode}));
