import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {runOfficeProcess} from '../src/main/office-process.js';

for (const reason of ['cancel','timeout'] as const) test(`Office ${reason} releases a blocked converter`, {timeout:15000}, async () => {
  const directory=await mkdtemp(join(tmpdir(),'astra-office-process-'));
  const controller=new AbortController();
  const args=['-e', "require('fs').writeFileSync('started', String(process.pid)); Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0)"];
  try {
    const blocked=runOfficeProcess(args,directory,controller.signal,reason==='timeout'?3000:10000);
    const receipt=assert.rejects(blocked, reason==='timeout'?{code:'timeout'}:/已取消/);
    let pid=0;
    const deadline=Date.now()+5000;
    while (!pid && Date.now()<deadline) {
      try {pid=Number(await readFile(join(directory,'started'),'utf8'));} catch {await delay(10);}
    }
    assert.ok(pid>0,'the converter started and reached the blocking operation');
    if (reason==='cancel') controller.abort();
    await receipt;
    assert.throws(()=>process.kill(pid,0),{code:'ESRCH'});
    assert.equal(await runOfficeProcess(['-p','42'],directory,new AbortController().signal,5000),'42\n');
  } finally {controller.abort();await rm(directory,{recursive:true,force:true});}
});
