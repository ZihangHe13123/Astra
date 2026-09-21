/** Desktop acceptance: real Python backend, isolated data, loopback-only model. */
import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const folder = mkdtempSync(join(tmpdir(), 'astra-desktop-smoke-'));
const workspace = join(folder, '工作区 with spaces'); mkdirSync(workspace);
const target = join(workspace, 'receipt.txt');
const output = join(root, 'output/playwright/gui'); mkdirSync(output, { recursive: true });
const requests = [];
const server = createServer(async (req, res) => {
  if (req.method === 'GET') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ data: [{ id: 'gui-test' }] })); return; }
  if (!req.url.endsWith('/chat/completions')) { res.writeHead(404); res.end(); return; }
  let body = ''; for await (const part of req) body += part;
  const payload = JSON.parse(body); requests.push(payload);
  const lastUser = payload.messages.findLastIndex(m => m.role === 'user');
  const text = String(payload.messages[lastUser]?.content || '');
  const answered = payload.messages.slice(lastUser + 1).some(m => m.role === 'tool');
  const call = !answered && text.includes('测试文件') ? ['write_file', { path: target, content: 'apple\norange\ngrape\n' }]
    : !answered && text.includes('追问') ? ['ask_user_question', { questions: [{ id: 'choice', question: '使用哪种格式？', multi_select: false,
      options: [{ label: 'Markdown', description: '便于阅读' }, { label: 'JSON', description: '便于处理' }] }] }] : undefined;
  res.setHeader('Content-Type', 'text/event-stream');
  const chunk = (delta, finish_reason = null) => res.write('data: ' + JSON.stringify({ id: 'smoke-' + requests.length,
    object: 'chat.completion.chunk', created: Math.floor(Date.now()/1000), model: 'gui-test',
    choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
  if (call) {
    chunk({ role: 'assistant', tool_calls: [{ index: 0, id: 'call-' + requests.length, type: 'function', function: { name: call[0], arguments: JSON.stringify(call[1]) } }] });
    chunk({}, 'tool_calls');
  } else {
    chunk({ role: 'assistant', reasoning_content: '先确认请求，再整理结果。' });
    if (text.includes('慢速A')) { chunk({ content: '会话 A 正在运行。' }); await new Promise(r=>setTimeout(r, 2500)); chunk({ content: '\n\n会话 A 已完成。' }); }
    else chunk({ content: answered ? '操作完成，结果已核验。' : '你好，Astra 桌面连接成功。\n\n| 项目 | 状态 |\n| --- | --- |\n| 后端 | 已连接 |\n\n```python\nprint("hello")\n```' });
    chunk({}, 'stop');
  }
  res.end('data: [DONE]\n\n');
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const port = server.address().port;
writeFileSync(join(folder, '.env'), '');
writeFileSync(join(folder, 'settings.json'), JSON.stringify({ selected_model: 'gui-test' }));
writeFileSync(join(folder, 'models.yaml'), `version: 1\nproviders:\n  test:\n    label: Local fixture\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\nmodels:\n  gui-test:\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\n`);
// A preset without credentials must lead to configuration, not an attempted switch.
const modelsPath=join(folder,'models.yaml');
writeFileSync(modelsPath,readFileSync(modelsPath,'utf8')+`  missing-model:\n    provider: openai-compatible\n    base_url: https://api.deepseek.com\n    api_key_env: GUI_MISSING_API_KEY\n    context_limit: 131072\n`);
const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/API_KEY|TOKEN|SECRET|^AGENT_|^ASTRA_|^LLM_|^SANDBOX_|^ELECTRON_/.test(key)));
const env = { ...cleanEnv, ASTRA_GUI_DISABLE_APPSHOT:'1', ASTRA_LOCAL_MODE_FILE:join(root,'tests/fixtures/local_mode.py'), AGENT_PROJECT_ROOT:root, AGENT_PYTHON:process.env.AGENT_PYTHON || join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python'),
 ASTRA_ENV_FILE:join(folder,'.env'), ASTRA_HOME:folder, AGENT_SETTINGS_PATH:join(folder,'settings.json'), AGENT_SESSION_DIR:join(folder,'sessions'),
 AGENT_TASK_DB:join(folder,'tasks.db'), ASTRA_APPROVAL_DB:join(folder,'approvals.db'), ASTRA_EVENT_DB:join(folder,'events.db'),
 AGENT_MEMORY_PATH:join(folder,'memory.db'), AGENT_LEARNING_PATH:join(folder,'learning.db'), AGENT_SKILLS_PATH:join(folder,'skills'),
 AGENT_MODELS_FILE:join(folder,'models.yaml'), AGENT_USER_MODELS_FILE:join(folder,'missing-models.yaml'), AGENT_LOG_DIR:join(folder,'logs'),
 AGENT_MCP_CONFIG:join(folder,'missing-mcp.json'), AGENT_TOOL_POLICY:'locked', SANDBOX_DOCKER:'false', SANDBOX_WORKDIR:workspace, ASTRA_WORKSPACE:workspace, LEARNING_REVIEW_AUTO:'0' };
let app;
const checks = [];
const passed = name => { checks.push(name); console.log('PASS', name); };
const snapshotName = (snapshot, id) => snapshot.sessions.find(s=>s.id===id).session;
try {
 app = await electron.launch({ args:[join(root,'ui-gui')], env, timeout:30000 });
 const page = await app.firstWindow();
 const errors = []; page.on('pageerror', e=>errors.push(e.message));
 const idle = async () => {
   const deadline = Date.now() + 30000;
   while (Date.now() < deadline) {
     const sessions = await page.evaluate(async () => (await window.astra.bootstrap()).sessions);
     if (sessions.length && sessions.every(s => !s.busy && !s.approvals.length && !s.questions.length
       && (!s.info.task_status?.task || ['completed', 'failed', 'cancelled'].includes(s.info.task_status.task.status)))) return;
     await new Promise(r => setTimeout(r, 100));
   }
   throw new Error('Backend tasks did not settle');
 };
 const submit = async text => { await page.getByRole('textbox',{name:'消息'}).fill(text); await page.getByRole('button',{name:'发送',exact:true}).click(); };
 await page.getByRole('textbox',{name:'消息'}).waitFor();
 await page.evaluate(()=>{window.__events=[];window.astra.onEvents(e=>window.__events.push(...e.map(x=>({type:x.event?.type,message:x.event?.message}))));});
 await page.screenshot({path:join(output,'welcome.png')}); passed('window and sandboxed preload');
 for(let i=0;i<8;i++) await page.getByRole('button',{name:'新对话',exact:false}).click();
 assert.equal((await page.evaluate(()=>window.astra.bootstrap())).sessions.length,0);
 await page.getByRole('textbox',{name:'消息'}).fill('空白页草稿');
 await page.getByRole('button',{name:'新对话',exact:false}).click();
 assert.equal(await page.getByRole('textbox',{name:'消息'}).inputValue(),'空白页草稿');
 passed('repeated New conversation keeps one local blank page and preserves draft');
 await page.locator('.model-button').click();
 await page.getByRole('dialog',{name:'选择模型',exact:true}).waitFor();
 await page.locator('.models button').filter({hasText:'missing-model'}).waitFor();
 await page.locator('.models button').filter({hasText:'missing-model'}).click();
 await page.getByRole('dialog',{name:'模型与账号'}).waitFor();
 for(let i=0;i<2;i++) await page.locator('.models button').filter({hasText:'missing-model'}).click();
 assert.equal(await page.getByLabel('提供商 / 计费路线').inputValue(),'deepseek');
 await page.getByText(/尚未连接。请在右侧登录/).waitFor();
 assert.equal(await page.locator('.message.error').count(),0);
 const blank=await page.evaluate(()=>window.astra.bootstrap());
 assert.equal(blank.sessions.length,1); assert.equal(blank.sessions[0].isDraft,true);
 assert.equal(blank.sessions[0].tools.length,0); assert.equal(requests.length,0);
 const failure=await page.evaluate(async()=>{
   const b=await window.astra.bootstrap(); const s=b.sessions[0];
   try {await window.astra.send(s.id,{type:'select_model',model_key:s.info.model_info.models.find(m=>m.name==='missing-model').key,request_id:'missing-test'});return '';}
   catch(e){return String(e);}
 });
 assert.match(failure,/Missing API key/);
 await page.waitForFunction(async()=> (await window.astra.bootstrap()).sessions[0].info.gui_model_result?.code==='provider_not_configured');
 assert.equal(await page.locator('.message.error').count(),0);
 await page.screenshot({path:join(output,'model-connection-guidance.png')});
 await page.getByRole('button',{name:'关闭',exact:true}).click();
 for(let i=0;i<8;i++) await page.getByRole('button',{name:'新对话',exact:false}).click();
 const ids=await page.evaluate(async()=>Promise.all(Array.from({length:5},()=>window.astra.create({}))).then(rs=>rs.map(r=>r.id)));
 assert.equal(new Set(ids).size,1); assert.equal((await page.evaluate(()=>window.astra.bootstrap())).sessions.length,1);
 assert.equal(await page.locator('.open-session').count(),0);
 assert.equal(await page.locator('.topbar .title').innerText(),'新对话');
 passed('missing credentials guide setup; failed selection stays local; concurrent setup reuses one draft backend');
 // Bind the reused host draft in the renderer before simulating disconnect.
 await page.locator('.model-button').click();
 await page.getByRole('dialog',{name:'选择模型',exact:true}).waitFor();
 await page.getByRole('button',{name:'关闭',exact:true}).click();
 // Fail before admission: an unpersisted draft must stay visible.
 // Explicit close now releases a runtime. Kill only this fixture's Python child
 // to simulate transport loss while preserving its renderer/runtime identity.
 const hostPid=await app.evaluate(()=>process.pid);
 const backendPids=process.platform==='win32'
   ? JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',`ConvertTo-Json -Compress -InputObject @(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq ${hostPid} -and $_.CommandLine -match '-m agent.cli.backend' } | ForEach-Object { $_.ProcessId })`],{encoding:'utf8'}))
   : execFileSync('ps',['-axo','pid=,ppid=,command='],{encoding:'utf8'}).split('\n').flatMap(line=>{
       const match=line.trim().match(/^(\d+)\s+(\d+)\s+(.+)$/);
       return match&&Number(match[2])===hostPid&&match[3].includes('-m agent.cli.backend')?[Number(match[1])]:[];
     });
 assert.equal(backendPids.length,1,'only the isolated draft backend should be running');
 process.kill(backendPids[0],'SIGKILL');
 await page.waitForFunction(async()=> (await window.astra.bootstrap()).sessions.every(s=>s.status==='disconnected'));
 await submit('发送失败仍保留的草稿');
 await page.getByRole('alert').filter({hasText:/Backend|后端/}).waitFor();
 assert.equal(await page.getByRole('textbox',{name:'消息'}).inputValue(),'发送失败仍保留的草稿');
 await page.getByRole('button',{name:'新对话',exact:false}).click();
 passed('pre-admission failure preserves visible blank-page draft');
 await submit('你好');
 await page.getByText('你好，Astra 桌面连接成功。',{exact:true}).waitFor({timeout:30000}); await idle();
 const first = (await page.evaluate(()=>window.astra.bootstrap())).active;
 assert.equal(requests.length, 1); passed('first message creates backend and executes once');
 await page.locator('.model-button').click(); await page.getByRole('dialog',{name:'选择模型',exact:true}).waitFor();
 await page.getByRole('button',{name:'模型与账号',exact:false}).click();
 await page.getByRole('dialog',{name:'模型与账号'}).waitFor();
 await page.getByLabel('提供商 / 计费路线').selectOption('codex');
 await page.getByRole('button',{name:'登录 ChatGPT',exact:true}).waitFor();
 await page.screenshot({path:join(output,'models.png')});
 await page.getByRole('button',{name:'关闭',exact:true}).click(); passed('ChatGPT login entry and model catalogue');
 await submit('请写入测试文件');
 await page.getByRole('button',{name:'允许一次',exact:true}).waitFor({timeout:15000});
 assert.equal(existsSync(target), false);
 await page.getByRole('button',{name:'允许一次',exact:true}).click(); await idle();
 assert.equal(readFileSync(target,'utf8'), 'apple\norange\ngrape\n'); passed('approval gates actual file write');
 await page.getByRole('button',{name:'执行详情',exact:true}).click(); await page.getByRole('tab',{name:'改动',exact:true}).click();
 await page.locator('.file-row').first().waitFor(); await page.locator('.file-row').first().click();
 await page.locator('.diff').waitFor(); await page.screenshot({path:join(output,'conversation-diff.png')});
 await page.getByRole('button',{name:'关闭详情',exact:true}).click(); passed('real change ledger and snapshots');
 await submit('请追问格式'); await page.getByRole('radio',{name:'Markdown 便于阅读'}).check();
 await page.getByRole('button',{name:'发送答复',exact:true}).click(); await idle(); passed('structured question and same-turn continuation');
 await submit('慢速A'); await page.getByText('会话 A 正在运行。',{exact:true}).waitFor();
 await page.getByRole('button',{name:'新对话',exact:false}).click();
 await page.waitForFunction(id => document.querySelector('.topbar .title')?.textContent !== id,
   snapshotName(await page.evaluate(()=>window.astra.bootstrap()), first));
 await submit('会话B');
 await page.getByText('你好，Astra 桌面连接成功。',{exact:true}).waitFor();
 assert.equal(await page.getByText('会话 A 已完成。',{exact:true}).count(), 0); await idle();
 const snapshot=await page.evaluate(()=>window.astra.bootstrap());
 assert.equal(snapshot.sessions.find(s=>s.id===first).messages.filter(m=>m.role==='assistant').at(-1).content.includes('会话 A 已完成。'),true);
 passed('two live backends preserve routing and background completion');
 await page.reload(); await page.getByRole('textbox',{name:'消息'}).waitFor();
 await page.getByText('你好，Astra 桌面连接成功。',{exact:true}).waitFor();
 assert.equal(await page.getByText('你好，Astra 桌面连接成功。',{exact:true}).count(),1); passed('renderer reload restores one copy of history');
 await page.getByRole('textbox',{name:'消息'}).fill('未发送草稿');
 await app.evaluate(({dialog}, path) => { dialog.showOpenDialog=async()=>({canceled:false,filePaths:[path]}); }, target);
 await page.getByRole('button',{name:'添加附件',exact:true}).click();
 await page.getByRole('button',{name:'移除附件',exact:true}).waitFor();
 await page.waitForFunction(async()=> {
   const b=await window.astra.bootstrap(); const s=b.sessions.find(x=>x.id===b.active); const k=`${s.mode}:${s.session}`;
   return b.preferences.drafts[k]==='未发送草稿' && b.preferences.attachments[k]?.length===1;
 });
 await page.reload(); await page.getByRole('textbox',{name:'消息'}).waitFor();
 assert.equal(await page.getByRole('textbox',{name:'消息'}).inputValue(),'未发送草稿');
 await page.getByRole('button',{name:'移除附件',exact:true}).click();
 passed('draft text and attachment list survive renderer reload');
 // Local commands never become a model prompt; special modes remain isolated.
 const countBeforeCommands = requests.length;
 await submit('/fixture new');
 await page.waitForFunction(()=>document.querySelector('.mode-badge')?.textContent==='local');
 await submit('本地模式会话'); await page.getByText('你好，Astra 桌面连接成功。',{exact:true}).waitFor(); await idle();
 await submit('/fixture leave');
 await page.waitForFunction(()=>document.querySelector('.mode-badge')?.textContent==='');
 assert.equal(requests.length,countBeforeCommands+1); passed('installation-local mode enter/leave uses commands and isolated history');
 await submit('慢速A'); await page.getByText('会话 A 正在运行。',{exact:true}).waitFor();
 await page.getByRole('button',{name:'停止',exact:true}).click(); await idle();
 const stopped=await page.evaluate(()=>window.astra.bootstrap());
 assert.equal(stopped.sessions.find(s=>s.id===stopped.active).busy,false); passed('cancel completes without deleting partial output');
 // Load a long active conversation through the real session path, then make
 // one loopback reply so its live reasoning row can leave and reenter the DOM.
 const activeHistoryName='desktop-active-window';
 writeFileSync(join(folder,'sessions',activeHistoryName+'.json'),JSON.stringify({messages:Array.from({length:240},(_,i)=>
   ({role:i%2?'assistant':'user',content:`活动历史 ${i}\n${'窗口锚点 '.repeat(35)}`,timestamp:1700100000+i}))}));
 await page.getByRole('button',{name:'刷新会话',exact:true}).click();
 await page.getByRole('button',{name:activeHistoryName,exact:true}).click();
 await page.getByRole('button',{name:'继续此会话',exact:false}).click();
 await page.waitForFunction(async name=>{
   const b=await window.astra.bootstrap();const s=b.sessions.find(s=>s.id===b.active);
   return s?.session===name&&s.status==='ready'&&s.messages.length===240;
 },activeHistoryName);
 await submit('虚拟列表状态测试'); await page.getByText('你好，Astra 桌面连接成功。',{exact:true}).waitFor(); await idle();
 const liveReasoning=page.locator('.reasoning').last();
 await liveReasoning.locator('summary').click();
 const reasoningId=await liveReasoning.evaluate(el=>el.closest('[data-message-id]').dataset.messageId);
 await page.waitForFunction(id=>[...document.querySelectorAll('[data-message-id]')].find(el=>el.dataset.messageId===id)?.querySelector('details')?.open,reasoningId);
 await page.locator('.messages-scroll').evaluate(el=>{el.scrollTop=0;});
 await page.waitForFunction(id=>![...document.querySelectorAll('[data-message-id]')].some(el=>el.dataset.messageId===id),reasoningId);
 await page.locator('.messages-scroll').evaluate(el=>{el.scrollTop=el.scrollHeight;});
 await page.waitForFunction(id=>[...document.querySelectorAll('[data-message-id]')].find(el=>el.dataset.messageId===id)?.querySelector('details')?.open,reasoningId);
 passed('reasoning expansion survives leaving and reentering the virtual message window');
 const priorBounds=await app.evaluate(({BrowserWindow})=>{
   const window=BrowserWindow.getAllWindows()[0];const bounds=window.getBounds();window.setBounds({...bounds,width:1000,height:800});return bounds;
 });
 await page.waitForFunction(()=>innerWidth<=1100);
 await page.locator('.messages-scroll').evaluate(el=>{el.scrollTop=(el.scrollHeight-el.clientHeight)*.45;});
 await page.waitForFunction(()=>{
   const el=document.querySelector('.messages-scroll');return el.scrollTop>el.clientHeight&&el.scrollHeight-el.scrollTop>el.clientHeight*2;
 });
 const middleAnchor=await page.evaluate(async()=>{
   // Wait for measured virtualization to settle after jumping into unmeasured
   // rows; the assertion below isolates the hide/show transition itself.
   let last = '', stable = 0;
   for (let frame = 0; frame < 120 && stable < 4; frame++) {
     await new Promise(resolve => requestAnimationFrame(resolve));
     const el = document.querySelector('.messages-scroll');
     const sample = `${el.scrollTop}:${el.scrollHeight}:${document.querySelector('.message')?.dataset.messageId}`;
     stable = sample === last ? stable + 1 : 0; last = sample;
   }
   const scroll=document.querySelector('.messages-scroll');const rect=scroll.getBoundingClientRect();
   const row=[...document.querySelectorAll('.message[data-message-id]')].find(el=>el.getBoundingClientRect().bottom>rect.top&&el.getBoundingClientRect().top<rect.bottom);
   return {id:row.dataset.messageId,top:row.getBoundingClientRect().top-rect.top};
 });
 await page.getByRole('button',{name:'执行详情',exact:true}).click();
 await page.locator('.conversation').waitFor({state:'hidden'});
 await page.getByRole('button',{name:'关闭详情',exact:true}).click();
 await page.waitForFunction(anchor=>{
   const scroll=document.querySelector('.messages-scroll');
   const row=[...document.querySelectorAll('.message[data-message-id]')].find(el=>el.dataset.messageId===anchor.id);
   return scroll.clientHeight>0&&row&&Math.abs(row.getBoundingClientRect().top-scroll.getBoundingClientRect().top-anchor.top)<8;
 },middleAnchor);
 await app.evaluate(({BrowserWindow},bounds)=>BrowserWindow.getAllWindows()[0].setBounds(bounds),priorBounds);
 passed('opening and closing details at 1000px preserves the active conversation reading anchor');
 // A scheduled reminder owns a live backend even while no model is running.
 // Exercise actual backend state, host close/delete guards and the window's
 // Return choice, then cancel immediately so the fixture never fires.
 const reminderRuntime=await page.evaluate(async()=>{
   const b=await window.astra.bootstrap();
   await window.astra.send(b.active,{type:'command',cmd:'/wakeup after 600 Desktop lifecycle fixture; do not run during this test.'});
   return b.active;
 });
 await page.waitForFunction(async id=>(await window.astra.bootstrap()).sessions.find(s=>s.id===id)?.info.wakeup_status?.plan?.state==='scheduled',reminderRuntime);
 const reminderRequests=requests.length;
 try {
   const guarded=await page.evaluate(async id=>{
     const original=(await window.astra.bootstrap()).sessions.find(s=>s.id===id);
     let closeError='',deleteError='';
     try {await window.astra.close(id);} catch(error) {closeError=String(error);}
     try {await window.astra.sessionAction('delete',{name:original.session,mode:original.mode});} catch(error) {deleteError=String(error);}
     const after=(await window.astra.bootstrap()).sessions.find(s=>s.id===id);
     return {closeError,deleteError,status:after?.status,plan:after?.info.wakeup_status?.plan?.state};
   },reminderRuntime);
   assert.match(guarded.closeError,/仍有任务或提醒/); assert.match(guarded.deleteError,/仍有任务或提醒/);
   assert.equal(guarded.status,'ready'); assert.equal(guarded.plan,'scheduled');
   const closePrompt=await app.evaluate(async({BrowserWindow,dialog})=>{
     const original=dialog.showMessageBox;const calls=[];const window=BrowserWindow.getAllWindows()[0];
     dialog.showMessageBox=async(...args)=>{calls.push(args.at(-1));return {response:2,checkboxChecked:false};};
     try {
       window.close();await new Promise(resolve=>setImmediate(resolve));
       return {calls,closed:window.isDestroyed(),visible:!window.isDestroyed()&&window.isVisible()};
     } finally {dialog.showMessageBox=original;}
   });
   assert.equal(closePrompt.calls.length,1); assert.match(closePrompt.calls[0].message,/仍有任务或会话提醒/);
   assert.equal(closePrompt.calls[0].buttons[2],'返回'); assert.equal(closePrompt.closed,false); assert.equal(closePrompt.visible,true);
   const retained=await page.evaluate(async id=>(await window.astra.bootstrap()).sessions.find(s=>s.id===id),reminderRuntime);
   assert.equal(retained.status,'ready'); assert.equal(retained.info.wakeup_status.plan.state,'scheduled');
 } finally {
   await page.evaluate(id=>window.astra.send(id,{type:'command',cmd:'/wakeup cancel'}),reminderRuntime);
   await page.waitForFunction(async id=>{
     const s=(await window.astra.bootstrap()).sessions.find(s=>s.id===id);
     return s&&!['scheduled','running','active'].includes(s.info.wakeup_status?.plan?.state);
   },reminderRuntime);
 }
 assert.equal(requests.length,reminderRequests);
 passed('real scheduled wakeup blocks close and delete, Return preserves the window, and cancel releases the plan');
 // Large history is browsed without creating a backend or invoking a model.
 const historyName='desktop-long-history';
 writeFileSync(join(folder,'sessions',historyName+'.json'),JSON.stringify({messages:Array.from({length:10000},(_,i)=>
   ({role:i%2?'assistant':'user',content:`历史消息 ${i}\n${'x'.repeat(2048)}`,timestamp:1700000000+i}))}));
 await page.getByRole('button',{name:'刷新会话',exact:true}).click();
 await page.getByRole('button',{name:historyName,exact:true}).waitFor();
 const measurements=[]; const beforeHistory=requests.length;
 for(let i=0;i<5;i++) {
   const prior = await page.locator('.messages-scroll').getAttribute('data-history-request');
   const start=performance.now(); await page.getByRole('button',{name:historyName,exact:true}).click();
   await page.waitForFunction(previous => document.querySelector('.messages-scroll')?.getAttribute('data-history-request') !== previous, prior);
   await page.getByText('只读浏览历史，不会启动模型。',{exact:true}).waitFor();
   await page.locator('.message-body').last().filter({hasText:'历史消息 9999'}).waitFor();
   measurements.push(Math.round(performance.now()-start));
 }
 assert.equal(requests.length,beforeHistory);
 assert.ok(await page.locator('.message').count()<=100);
 passed('10,000-message history is read-only and mounts at most 100 messages');
 await page.screenshot({path:join(output,'long-history.png')});
 // Prepending a page keeps the current reading position and makes the added
 // messages reachable by scrolling, without increasing the mounted window.
 await page.locator('.messages-scroll').evaluate(el=>{el.scrollTop=0;});
 await page.getByRole('button',{name:'加载更早历史',exact:true}).waitFor();
 await page.waitForFunction(()=>{
   const scroll=document.querySelector('.messages-scroll');
   const first=document.querySelector('.message[data-message-id]');
   return scroll&&first&&scroll.scrollTop===0&&first.getBoundingClientRect().top<scroll.getBoundingClientRect().bottom;
 });
 const historyAnchor=await page.evaluate(()=>{
   const scroll=document.querySelector('.messages-scroll'); const first=document.querySelector('.message[data-message-id]');
   return {id:first.dataset.messageId,top:first.getBoundingClientRect().top-scroll.getBoundingClientRect().top,
     loaded:Number(document.querySelector('.message-window').dataset.totalMessages),text:first.textContent};
 });
 await page.getByRole('button',{name:'加载更早历史',exact:true}).click();
 await page.waitForFunction(anchor=>{
   const scroll=document.querySelector('.messages-scroll');
   const row=[...document.querySelectorAll('.message[data-message-id]')].find(el=>el.dataset.messageId===anchor.id);
   return Number(document.querySelector('.message-window')?.dataset.totalMessages)>anchor.loaded&&row&&
     Math.abs(row.getBoundingClientRect().top-scroll.getBoundingClientRect().top-anchor.top)<8;
 },historyAnchor);
 await page.locator('.messages-scroll').evaluate(el=>{el.scrollTop=0;});
 await page.waitForFunction(old=>{
   const first=document.querySelector('.message[data-message-id]');
   return first&&Number(first.dataset.messageId.split(':').at(-1))<Number(old.split(':').at(-1));
 },historyAnchor.id);
 assert.equal(requests.length,beforeHistory); assert.ok(await page.locator('.message').count()<=100);
 passed('older history becomes reachable and pagination preserves the visible message anchor');
 // An external rewrite invalidates the old pagination cursor. The GUI must
 // replace the loaded generation instead of prepending new rows to old text.
 writeFileSync(join(folder,'sessions',historyName+'.json'),JSON.stringify({messages:Array.from({length:3},(_,i)=>
   ({role:i%2?'assistant':'user',content:`重写后的历史 ${i}`,timestamp:1700200000+i}))}));
 await page.locator('.messages-scroll').evaluate(el=>{el.scrollTop=0;});
 await page.getByRole('button',{name:'加载更早历史',exact:true}).click();
 await page.getByRole('alert').filter({hasText:'历史已在其他窗口更新，已载入最新记录。'}).waitFor();
 await page.getByText('重写后的历史 2',{exact:true}).waitFor();
 assert.equal(await page.locator('.message-window').getAttribute('data-total-messages'),'3');
 assert.equal(await page.locator('.message-body').filter({hasText:/历史消息 \d+/}).count(),0);
 assert.equal(await page.getByRole('button',{name:'加载更早历史',exact:true}).count(),0);
 assert.equal(requests.length,beforeHistory);
 passed('history rewritten outside the GUI replaces the loaded generation without mixing old pages');
 // Native controls must target the visible historical session, not a hidden live one.
 const menuName='desktop-menu-history';
 const menuPath=join(folder,'sessions',menuName+'.json');
 writeFileSync(menuPath,JSON.stringify({messages:[{role:'user',content:'菜单测试历史',timestamp:1700000000},{role:'assistant',content:'历史答复',timestamp:1700000001}]}));
 await page.getByRole('button',{name:'刷新会话',exact:true}).click();
 await page.getByRole('button',{name:menuName,exact:true}).click();
 await page.getByText('只读浏览历史，不会启动模型。',{exact:true}).waitFor();
 assert.equal((await page.evaluate(()=>window.astra.bootstrap())).active,'');
 const oldPermissions=await page.evaluate(async()=> (await window.astra.bootstrap()).sessions.map(s=>[s.id,!!s.info.yolo_status?.yolo]));
 const palette = async filter => {
   await page.getByRole('button',{name:'命令与功能',exact:false}).click();
   const search=page.getByRole('combobox',{name:'搜索命令与功能'});
   await search.waitFor(); assert.equal(await search.evaluate(el=>el===document.activeElement),true);
   const first=await search.getAttribute('aria-activedescendant');
   await search.press('ArrowDown'); assert.notEqual(await search.getAttribute('aria-activedescendant'),first);
   await search.press('ArrowUp'); assert.equal(await search.getAttribute('aria-activedescendant'),first);
   await search.fill(filter); await search.press('Enter');
 };
 await palette('/permissions');
 const permissionDialog=page.getByRole('dialog',{name:'权限与对话模式'});
 await permissionDialog.waitFor();
 await permissionDialog.getByRole('button',{name:'完全访问',exact:false}).click();
 await page.waitForFunction(async name=>{const b=await window.astra.bootstrap();const s=b.sessions.find(s=>s.id===b.active);return s?.session===name && s.info.yolo_status?.yolo===true;},menuName);
 const afterPermissions=await page.evaluate(()=>window.astra.bootstrap());
 for(const [id,value] of oldPermissions) assert.equal(!!afterPermissions.sessions.find(s=>s.id===id).info.yolo_status?.yolo,value);
 assert.equal(requests.length,beforeHistory);
 passed('palette opens permissions and acts on visible history without changing hidden sessions');
 await palette('/persona'); await page.getByRole('dialog',{name:'人格设置'}).waitFor();
 await page.getByRole('button',{name:'关闭',exact:true}).click();
 await palette('/session'); await page.getByRole('dialog',{name:'管理会话'}).waitFor();
 await page.getByRole('button',{name:'关闭',exact:true}).click();
 passed('command palette transfers to native persona and session panels');
 // Rename is a display label, preserving backend identity and on-disk history.
 const menu=async title=>{await page.getByRole('button',{name:`会话操作：${title}`,exact:true}).first().click();await page.getByRole('menu',{name:'会话操作'}).waitFor();};
 await menu(menuName); await page.getByRole('menuitem',{name:'重命名',exact:true}).click();
 await page.getByLabel('会话名称',{exact:true}).fill('改名后的会话');
 await page.getByRole('button',{name:'保存名称',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.topbar .title')?.textContent==='改名后的会话');
 assert.ok(existsSync(menuPath));
 await menu('改名后的会话'); await page.getByRole('menuitem',{name:'置顶',exact:true}).click();
 await page.waitForFunction(async name=>(await window.astra.bootstrap()).preferences.pinned.includes('work:'+name),menuName);
 const exported=join(folder,'exported-conversation.md');
 await app.evaluate(({dialog,shell},filePath)=>{dialog.showSaveDialog=async()=>({canceled:false,filePath});shell.showItemInFolder=()=>{};},exported);
 await menu('改名后的会话'); await page.getByRole('menuitem',{name:'导出 Markdown',exact:true}).click();
 await page.getByRole('alert').filter({hasText:'已导出'}).waitFor();
 assert.match(readFileSync(exported,'utf8'),/菜单测试历史/); assert.match(readFileSync(exported,'utf8'),/历史答复/);
 await menu('改名后的会话'); await page.screenshot({path:join(output,'session-menu.png')});
 await page.getByRole('menuitem',{name:'删除会话',exact:true}).click();
 await page.getByRole('button',{name:'取消',exact:true}).click(); assert.ok(existsSync(menuPath));
 await menu('改名后的会话'); await page.getByRole('menuitem',{name:'删除会话',exact:true}).click();
 await page.getByRole('button',{name:'确认删除',exact:true}).click();
 await page.getByRole('dialog',{name:'删除会话'}).waitFor({state:'hidden'});
 assert.equal(existsSync(menuPath),false); assert.ok(existsSync(target));
 assert.equal(await page.getByRole('button',{name:'会话操作：改名后的会话',exact:true}).count(),0);
 passed('session menu renames, pins, exports, cancels deletion and deletes only the confirmed history');
 // Empty special modes are real namespaces; New must return to a generic blank page.
 await page.getByRole('button',{name:'按需审批',exact:true}).click();
 await page.getByRole('dialog',{name:'权限与对话模式'}).getByRole('button',{name:'Fixture',exact:false}).click();
 await page.waitForFunction(()=>document.querySelector('.mode-badge')?.textContent==='local');
 await page.getByRole('button',{name:'按需审批',exact:true}).click();
 assert.equal(await page.getByRole('dialog',{name:'权限与对话模式'}).getByRole('button',{name:'极简',exact:false}).isDisabled(),true);
 await page.getByText('先返回通用模式，再选择其他模式。',{exact:true}).waitFor();
 await page.getByRole('button',{name:'关闭',exact:true}).click();
 await page.getByRole('button',{name:'新对话',exact:false}).click();
 assert.equal(await page.locator('.mode-badge').innerText(),'');
 assert.equal(await page.locator('.topbar .title').innerText(),'新对话');
 passed('native mode controls respect existing isolation; New leaves empty local session for generic draft');
 const beforeClose=await page.evaluate(()=>window.astra.bootstrap());
 const closed=beforeClose.sessions.find(s=>s.id===first);
 assert.ok(closed);
 await page.evaluate(id=>window.astra.close(id),first);
 await page.waitForFunction(async id=>!(await window.astra.bootstrap()).sessions.some(s=>s.id===id),first);
 const closedHistory=await page.evaluate(async entry=>({
   sessions:await window.astra.query('sessions'),
   history:await window.astra.query('history',{name:entry.session,mode:entry.mode}),
 }),closed);
 assert.ok(closedHistory.sessions.some(entry=>entry.name===closed.session&&entry.mode===closed.mode));
 assert.ok(closedHistory.history.messages.some(message=>String(message.content).includes('你好')));
 passed('explicit close releases the runtime while preserving its saved history');
 // Full host shutdown/relaunch restores the last session and the final keystroke,
 // without invoking the model. This is separate from a renderer-only reload.
 await page.getByRole('button',{name:'新对话',exact:false}).click();
 await submit('重启恢复测试'); await page.getByText('你好，Astra 桌面连接成功。',{exact:true}).waitFor(); await idle();
 const beforeRelaunch=requests.length;
 const last=await page.evaluate(async()=>{const b=await window.astra.bootstrap();return b.sessions.find(s=>s.id===b.active).session;});
 await page.getByRole('textbox',{name:'消息'}).fill('退出前的草稿');
 await app.close(); app=undefined;
 app=await electron.launch({args:[join(root,'ui-gui')],env,timeout:30000});
 const restored=await app.firstWindow(); restored.on('pageerror',e=>errors.push(e.message));
 await restored.waitForFunction(async name=>{const b=await window.astra.bootstrap();return b.sessions.some(s=>s.session===name&&s.status==='ready');},last);
 assert.equal(await restored.getByRole('textbox',{name:'消息'}).inputValue(),'退出前的草稿');
 await restored.getByText('你好，Astra 桌面连接成功。',{exact:true}).waitFor();
 assert.equal(requests.length,beforeRelaunch); passed('full desktop relaunch restores history and draft without requesting a model');
 await restored.evaluate(()=>{window.__restarted=false;window.astra.onEvents(events=>{if(events.some(e=>e.event?.type==='backend_hello'))window.__restarted=true;});});
 await restored.getByRole('textbox',{name:'消息'}).fill('/restart');
 await restored.getByRole('button',{name:'发送',exact:true}).click();
 await restored.waitForFunction(async()=>window.__restarted&&(await window.astra.bootstrap()).sessions.some(s=>s.status==='ready'),null,{timeout:30000});
 await restored.getByText('你好，Astra 桌面连接成功。',{exact:true}).waitFor();
 assert.equal(await restored.getByText('你好，Astra 桌面连接成功。',{exact:true}).count(),1);
 assert.equal(requests.length,beforeRelaunch); passed('controlled backend restart preserves history and does not resend the model request');
 assert.deepEqual(errors, []); passed('no renderer exceptions');
 writeFileSync(join(output,'result.json'),JSON.stringify({checks,isolatedData:folder,historyMs:measurements,platform:process.platform},null,2));
 console.log('Artifacts:',output);
} catch (error) {
 console.error('SMOKE FAILED',error);
 if (app) { try {const p=await app.firstWindow();await p.screenshot({path:join(output,'failure.png')});console.error((await p.locator('body').innerText()).slice(-6000));console.error('State',await p.evaluate(async()=>{const b=await window.astra.bootstrap();return b.sessions.map(s=>({status:s.status,info:Object.keys(s.info),notices:s.notices}));}));} catch {} }
 console.error('Isolated diagnostic data:',folder); process.exitCode=1;
} finally {
 if(app) await app.close();
 server.closeAllConnections();await new Promise(r=>server.close(r));
}
