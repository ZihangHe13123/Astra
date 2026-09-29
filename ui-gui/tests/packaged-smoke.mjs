/** Execute the relocated app with an empty PATH and no developer interpreter. */
import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtempSync, mkdirSync, cpSync, writeFileSync, readFileSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { execFileSync } from 'node:child_process';

const release = resolve(process.argv[2] || '');
const manifest = JSON.parse(readFileSync(join(release, 'release.json'), 'utf8'));
const test = realpathSync(mkdtempSync(join(tmpdir(), 'astra-standalone-')));
const relocated = join(test, 'relocated application', manifest.application);
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
const imports=execFileSync(python,['-I','-B','-c','import sys,ssl,sqlite3,docx,pptx,openpyxl,pypdf,PIL,json; print(json.dumps({"python":sys.executable,"prefix":sys.prefix,"version":sys.version}))'],{cwd:workspace,env,encoding:'utf8'});
assert.ok(JSON.parse(imports).python.startsWith(relocated));
let requests=0;
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
let app;
try{
 app=await electron.launch({executablePath:join(relocated,process.platform==='darwin'?'Contents/MacOS/Astra':'Astra.exe'),env,timeout:45000});
 const page=await app.firstWindow();
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.getByRole('textbox',{name:'消息'}).waitFor({timeout:30000});
 const paths=await app.evaluate(()=>({packaged:process.resourcesPath,python:process.env.AGENT_PYTHON,backend:process.env.AGENT_PROJECT_ROOT,data:process.env.ASTRA_HOME,path:process.env.PATH}));
 assert.equal(paths.python,realpathSync(python));
 assert.equal(paths.backend,backend);
 assert.equal(paths.data,data);
 assert.equal(paths.path,'');
 await page.getByRole('textbox',{name:'消息'}).fill('Reply with the runtime receipt.');
 await page.getByRole('button',{name:'发送',exact:true}).click();
 await page.getByText('PACKAGED_RUNTIME_OK',{exact:true}).waitFor({timeout:45000});
 assert.ok(requests>0);
 assert.deepEqual(errors,[]);
 await page.screenshot({path:join(test,'standalone.png')});
}finally{
 if(app)await app.close();
 server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
}
// -I ignores PYTHONDONTWRITEBYTECODE, so the import preflight uses -B too.
// Assert that neither the preflight nor the running GUI modifies its bundle.
execFileSync(python,['-P','-B','-c',
 'import json,sys; from pathlib import Path; from agent.launcher.desktop_distribution import verify_tree; verify_tree(Path(sys.argv[1]),json.loads(Path(sys.argv[2]).read_text()))',
 relocated,join(release,'release.json')],{cwd:workspace,env:{...env,PYTHONPATH:backend},encoding:'utf8'});
console.log(JSON.stringify({ok:true,relocated,python:JSON.parse(imports),requests,immutable_bundle:true,screenshot:join(test,'standalone.png')},null,2));
