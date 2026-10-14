/** Execute the relocated app with an empty PATH and no developer interpreter. */
import { _electron as electron } from 'playwright';
import { expect } from '@playwright/test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtempSync, mkdirSync, cpSync, writeFileSync, readFileSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { execFile, execFileSync } from 'node:child_process';
import { promisify } from 'node:util';

const runFile = promisify(execFile);
async function bounded(promise, label, timeout) {
 let timer;
 try { return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error(`${label} timed out after ${timeout} ms`)),timeout);})]); }
 finally { clearTimeout(timer); }
}
async function terminateFixture(child) {
 if(child.exitCode!==null||child.signalCode!==null)return;
 const pid=child.pid;assert.ok(Number.isSafeInteger(pid)&&pid>0,'only terminate this fixture process tree');
 if(process.platform==='win32'){await runFile('taskkill.exe',['/PID',String(pid),'/T','/F'],{timeout:5000,windowsHide:true});return;}
 const {stdout}=await runFile('ps',['-axo','pid=,ppid='],{timeout:3000});
 const rows=stdout.trim().split('\n').map(line=>line.trim().split(/\s+/).map(Number)),owned=new Set([pid]);
 for(let changed=true;changed;){changed=false;for(const [id,parent]of rows)if(owned.has(parent)&&!owned.has(id)){owned.add(id);changed=true;}}
 for(const id of [...owned].reverse())try{process.kill(id,'SIGKILL');}catch(error){if(error.code!=='ESRCH')throw error;}
}

const release = resolve(process.argv[2] || '');
const manifest = JSON.parse(readFileSync(join(release, 'release.json'), 'utf8'));
const test = realpathSync(mkdtempSync(join(process.env.RUNNER_TEMP || tmpdir(), 'astra-standalone-')));
let app,fixtureProcess,failure,requests=0;
const diagnostics={phases:[],errors:[]};
function saveDiagnostics(){
 const child=fixtureProcess;
 writeFileSync(join(test,'packaged-diagnostics.json'),JSON.stringify({...diagnostics,requests,
  fixtureProcess:child?{pid:child.pid,exitCode:child.exitCode,signalCode:child.signalCode}:null},null,2));
}
function phase(name){diagnostics.phases.push({name,at:new Date().toISOString()});console.log(`PACKAGED_PHASE ${name}`);saveDiagnostics();}
function recordFailure(error){failure ||= error;diagnostics.errors.push(String(error?.message||error).slice(0,1000));console.error(error);saveDiagnostics();}
const relocated = join(test, 'relocated application', manifest.application);
phase('relocate');
mkdirSync(join(test,'relocated application'));
cpSync(join(release,manifest.application),relocated,{recursive:true,dereference:false,verbatimSymlinks:true});
const resources=join(relocated,process.platform==='darwin'?'Contents/Resources':'resources');
const runtime=join(resources,'runtime');
const descriptor=JSON.parse(readFileSync(join(runtime,'runtime.json'),'utf8'));
const python=join(runtime,descriptor.python);
const backend=join(runtime,'backend');
const data=join(test,'profile');mkdirSync(data);
const workspace=join(test,'工作区 with spaces');mkdirSync(workspace);
mkdirSync(join(workspace,'agent'));
writeFileSync(join(workspace,'agent/__init__.py'),'raise RuntimeError("Workspace code shadowed the bundled backend")\n');
const env = Object.fromEntries(['HOME','USER','LOGNAME','TMPDIR','TEMP','TMP','SystemRoot','SYSTEMROOT','WINDIR','COMSPEC','LOCALAPPDATA','APPDATA'].filter(key=>process.env[key]).map(key=>[key,process.env[key]]));
Object.assign(env,{PATH:'',ASTRA_HOME:data,ASTRA_WORKSPACE:workspace,ASTRA_GUI_DISABLE_APPSHOT:'1',AGENT_MODELS_FILE:join(data,'models.yaml'),AGENT_USER_MODELS_FILE:join(data,'no-user-models.yaml'),AGENT_MCP_CONFIG:join(data,'no-mcp.json'),SANDBOX_DOCKER:'false',LEARNING_REVIEW_AUTO:'0'});
phase('bundled-python-imports');
const imports=execFileSync(python,['-I','-B','-c','import sys,ssl,sqlite3,docx,pptx,openpyxl,pypdf,PIL,json; print(json.dumps({"python":sys.executable,"prefix":sys.prefix,"version":sys.version}))'],{cwd:workspace,env,encoding:'utf8',timeout:120000,killSignal:'SIGKILL'});
assert.ok(JSON.parse(imports).python.startsWith(relocated));
const server=createServer(async(req,res)=>{
 let body='';for await(const chunk of req)body+=chunk;
 if(req.method==='GET'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({data:[{id:'packaged-test'}]}));return;}
 requests++;
 const payload=JSON.parse(body);
 const msg={role:'assistant',content:'PACKAGED_RUNTIME_OK'};
 if(payload.stream){
  res.setHeader('Content-Type','text/event-stream');
  for(const [delta,finish_reason]of [[msg,null],[{},'stop']])res.write('data: '+JSON.stringify({id:'packaged-smoke',object:'chat.completion.chunk',model:'packaged-test',choices:[{index:0,delta,finish_reason}]})+'\n\n');
  res.end('data: [DONE]\n\n');
 }else{res.setHeader('Content-Type','application/json');res.end(JSON.stringify({id:'packaged-smoke',object:'chat.completion',model:'packaged-test',choices:[{index:0,message:msg,finish_reason:'stop'}]}));}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
writeFileSync(join(data,'.env'),'');
writeFileSync(join(data,'settings.json'),JSON.stringify({selected_model:'packaged-test'}));
writeFileSync(join(data,'models.yaml'),`version: 1\nmodels:\n  packaged-test:\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${server.address().port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\n`);
try{
 phase('launch');
 app=await electron.launch({executablePath:join(relocated,process.platform==='darwin'?'Contents/MacOS/Astra':'Astra.exe'),env,timeout:45000});
 fixtureProcess=app.process();
 const page=await app.firstWindow();
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 phase('runtime-paths');
 await page.getByRole('textbox',{name:'消息'}).waitFor({timeout:30000});
 const paths=await bounded(app.evaluate(()=>({packaged:process.resourcesPath,python:process.env.AGENT_PYTHON,backend:process.env.AGENT_PROJECT_ROOT,data:process.env.ASTRA_HOME,path:process.env.PATH})),'Runtime paths',30000);
 assert.equal(paths.python,realpathSync(python));
 assert.equal(paths.backend,backend);
 assert.equal(paths.data,data);
 // Windows can report an explicitly empty environment variable as absent.
 if(process.platform!=='win32'||paths.path!==undefined)assert.equal(paths.path,'');
 phase('model-receipt');
 await page.getByRole('textbox',{name:'消息'}).fill('Reply with the runtime receipt.');
 await page.getByRole('button',{name:'发送',exact:true}).click();
 await page.getByText('PACKAGED_RUNTIME_OK',{exact:true}).waitFor({timeout:45000});
 assert.ok(requests>0);
 // Visible streaming text is not a completed turn. Wait for the same state
 // used by quit admission, then read the saved reply through the history API.
 phase('idle-and-saved-receipt');
 await bounded(expect.poll(()=>page.evaluate(async()=>{
  const boot=await window.astra.bootstrap(),current=boot.sessions.find(item=>item.id===boot.active);
  if(!current||current.status!=='ready'||current.busy||current.approvals.length||current.questions.length)return false;
  const task=current.info.task_status?.task;
  if(task&&!['completed','failed','cancelled'].includes(task.status))return false;
  const saved=await window.astra.query('history',{name:current.session,mode:current.mode});
  return saved.messages.some(message=>message.role==='assistant'&&message.content==='PACKAGED_RUNTIME_OK');
 }),{timeout:30000}).toBe(true),'Idle and saved runtime receipt',35000);
 assert.deepEqual(errors,[]);
 phase('screenshot');
 await page.screenshot({path:join(test,'standalone.png')});
}catch(error){recordFailure(error);
}finally{
 if(app){
  phase('close');
  try{await bounded(app.close(),'Electron close',30000);}
  catch(error){recordFailure(error);phase('forced-fixture-cleanup');try{await bounded(terminateFixture(fixtureProcess),'Fixture process cleanup',6000);}catch(cleanup){recordFailure(cleanup);}}
 }
 phase('fixture-listener-close');
 server.closeAllConnections();await bounded(new Promise((resolve,reject)=>server.close(error=>error?reject(error):resolve())),'Fixture HTTP close',3000).catch(recordFailure);
}
if(failure)throw failure;
// -I ignores PYTHONDONTWRITEBYTECODE, so the import preflight uses -B too.
// Assert that neither the preflight nor the running GUI modifies its bundle.
phase('verify-immutable-bundle');
execFileSync(python,['-P','-B','-c',
 'import json,sys; from pathlib import Path; from agent.launcher.desktop_distribution import verify_tree; verify_tree(Path(sys.argv[1]),json.loads(Path(sys.argv[2]).read_text()))',
 relocated,join(release,'release.json')],{cwd:workspace,env:{...env,PYTHONPATH:backend},encoding:'utf8',timeout:120000,killSignal:'SIGKILL'});
phase('passed');
console.log(JSON.stringify({ok:true,relocated,python:JSON.parse(imports),requests,immutable_bundle:true,screenshot:join(test,'standalone.png')},null,2));
