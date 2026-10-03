import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, realpathSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { configurePackagedRuntime } from '../src/main/packaged-runtime.js';

test('packaged runtime pins its interpreter, separates writable data and drops Python injection', () => {
  const root = realpathSync(mkdtempSync(join(tmpdir(), 'astra-packaged-')));
  try {
    const resources = join(root, 'Astra.app/Contents/Resources');
    mkdirSync(join(resources, 'runtime/python/bin'), {recursive:true});
    mkdirSync(join(resources, 'runtime/backend'), {recursive:true});
    writeFileSync(join(resources, 'runtime/python/bin/python3'), 'python');
    writeFileSync(join(resources, 'runtime/runtime.json'), JSON.stringify({schema:1,distribution:'astra-desktop',version:'0.2.0',target:'darwin-arm64',python:'python/bin/python3',helper:null}));
    const env: NodeJS.ProcessEnv = {AGENT_PYTHON:'/evil/python',AGENT_PROJECT_ROOT:'/checkout',PYTHONPATH:'/evil',PYTHONHOME:'/evil',VIRTUAL_ENV:'/venv', ASTRA_HOME:join(root,'profile')};
    const options = {isPackaged:true,resourcesPath:resources,appData:join(root,'data'),platform:'darwin',arch:'arm64'};
    const runtime = configurePackagedRuntime(options,env)!;
    assert.equal(runtime.python,join(resources,'runtime/python/bin/python3'));
    assert.equal(env.PYTHONHOME,undefined);
    assert.equal(env.PYTHONPATH,runtime.root);
    assert.equal(env.ASTRA_ENV_FILE,join(runtime.data,'.env'));
    assert.equal(env.ASTRA_COMPUTER_HELPER_PATH,join(resources,'runtime/native/unavailable'));
    assert.equal(env.PYTHONDONTWRITEBYTECODE,'1');
    assert.equal(env.ASTRA_DESKTOP_RUNTIME,'1');
    assert.throws(()=>configurePackagedRuntime({...options,arch:'x64'},env),/target/);
    assert.throws(()=>configurePackagedRuntime(options,{ASTRA_HOME:resources}),/outside/);
  } finally {rmSync(root,{recursive:true,force:true});}
});

test('source desktop setup is not rewritten',()=>{
  const env={AGENT_PYTHON:'/my/venv'};
  assert.equal(configurePackagedRuntime({isPackaged:false,resourcesPath:'',appData:'',platform:'darwin',arch:'arm64'},env),undefined);
  assert.deepEqual(env,{AGENT_PYTHON:'/my/venv'});
});

test('packaged lease rejects a missing interpreter without hanging', async()=>{
  const { acquirePackagedLease }=await import('../src/main/packaged-runtime.js');
  await assert.rejects(acquirePackagedLease({root:tmpdir(),python:join(tmpdir(),'missing-astra-python'),data:tmpdir()}),/ENOENT|lease/);
});

test('packaged lease forces bounded shutdown when the child ignores EOF', {skip:process.platform==='win32'}, async()=>{
  const { acquirePackagedLease }=await import('../src/main/packaged-runtime.js');
  const root=realpathSync(mkdtempSync(join(tmpdir(),'astra-lease-test-')));
  const python=join(root,'lease');
  try {
    writeFileSync(python,'#!/usr/bin/python3\nimport time\nprint(\'{"ready":true}\',flush=True)\ntime.sleep(2)\n',{mode:0o755});
    const lease=await acquirePackagedLease({root,python,data:root},{graceMs:30,stopMs:30});
    const started=Date.now();await lease.close();
    assert.ok(Date.now()-started<1000,'lease close must terminate an EOF-unresponsive child');
  } finally {rmSync(root,{recursive:true,force:true});}
});

test('packaged user profile cannot be placed inside its application bundle',()=>{
 const root=realpathSync(mkdtempSync(join(tmpdir(),'astra-app-profile-')));
 const resources=join(root,'Astra.app/Contents/Resources');
 try{
  mkdirSync(join(resources,'runtime/python/bin'),{recursive:true});mkdirSync(join(resources,'runtime/backend'),{recursive:true});
  writeFileSync(join(resources,'runtime/python/bin/python3'),'python');
  writeFileSync(join(resources,'runtime/runtime.json'),JSON.stringify({schema:1,distribution:'astra-desktop',target:'darwin-arm64',python:'python/bin/python3',helper:null}));
  assert.throws(()=>configurePackagedRuntime({isPackaged:true,resourcesPath:resources,appData:root,platform:'darwin',arch:'arm64'},{ASTRA_HOME:join(root,'Astra.app/profile')}),/outside/);
 }finally{rmSync(root,{recursive:true,force:true});}
});

test('loss of an acquired lease is reported to the host', {skip:process.platform==='win32'}, async()=>{
 const {acquirePackagedLease}=await import('../src/main/packaged-runtime.js');
 const root=realpathSync(mkdtempSync(join(tmpdir(),'astra-lease-loss-')));const python=join(root,'lease');
 try{
  writeFileSync(python,'#!/usr/bin/python3\nimport time\nprint(\'{"ready":true}\',flush=True)\ntime.sleep(0.05)\n',{mode:0o755});
  let reportLost!: (error: Error) => void;
  const lost=new Promise<Error>(resolve=>{reportLost=resolve;});
  const lease=await acquirePackagedLease({root,python,data:root},{onLost:reportLost});
  let timeout: ReturnType<typeof setTimeout> | undefined;
  try {
   const error=await Promise.race([lost,new Promise<never>((_,reject)=>{
    timeout=setTimeout(()=>reject(new Error('Acquired lease loss was not reported within 5 seconds')),5000);
   })]);
   assert.match(String(error),/lease.*exit|lease.*lost/i);
  } finally {clearTimeout(timeout);await lease.close();}
 }finally{rmSync(root,{recursive:true,force:true});}
});
