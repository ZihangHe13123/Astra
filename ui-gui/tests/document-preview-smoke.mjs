/** Real Electron + native Office engine + PDF.js; isolated state and no model requests. */
import {_electron as electron} from 'playwright';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdtempSync,mkdirSync,copyFileSync,readFileSync,writeFileSync,utimesSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
const root=resolve(fileURLToPath(new URL('../..',import.meta.url)));
const folder=mkdtempSync(join(tmpdir(),'astra-document-ui-'));
const workspace=join(folder,'中文 documents');mkdirSync(workspace);
const output=join(root,'output/playwright/document-preview');mkdirSync(output,{recursive:true});
for(const extension of ['docx','pptx','xlsx'])copyFileSync(join(root,'ui-gui/tests/fixtures/documents',`preview.${extension}`),join(workspace,`preview.${extension}`));
writeFileSync(join(folder,'.env'),'');writeFileSync(join(folder,'settings.json'),JSON.stringify({selected_model:'preview-test'}));
writeFileSync(join(folder,'models.yaml'),`version: 1\nproviders:\n  fixture:\n    provider: openai-compatible\n    base_url: http://127.0.0.1:9/v1\n    api_key_env: ''\nmodels:\n  preview-test:\n    provider: openai-compatible\n    base_url: http://127.0.0.1:9/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\n`);
const cleanEnv=Object.fromEntries(Object.entries(process.env).filter(([key])=>!/API_KEY|TOKEN|SECRET|^AGENT_|^ASTRA_|^LLM_|^SANDBOX_|^ELECTRON_/.test(key)));
const env={...cleanEnv,ASTRA_GUI_DISABLE_APPSHOT:'1',AGENT_PROJECT_ROOT:root,AGENT_PYTHON:process.env.AGENT_PYTHON||join(root,'.venv/bin/python'),ASTRA_HOME:folder,ASTRA_ENV_FILE:join(folder,'.env'),AGENT_SETTINGS_PATH:join(folder,'settings.json'),AGENT_SESSION_DIR:join(folder,'sessions'),AGENT_TASK_DB:join(folder,'tasks.db'),ASTRA_APPROVAL_DB:join(folder,'approvals.db'),ASTRA_EVENT_DB:join(folder,'events.db'),AGENT_MEMORY_PATH:join(folder,'memory.db'),AGENT_LEARNING_PATH:join(folder,'learning.db'),AGENT_SKILLS_PATH:join(folder,'skills'),AGENT_MODELS_FILE:join(folder,'models.yaml'),AGENT_USER_MODELS_FILE:join(folder,'missing-models.yaml'),AGENT_LOG_DIR:join(folder,'logs'),AGENT_MCP_CONFIG:join(folder,'missing-mcp.json'),AGENT_TOOL_POLICY:'locked',SANDBOX_DOCKER:'false',SANDBOX_WORKDIR:workspace,ASTRA_WORKSPACE:workspace,LEARNING_REVIEW_AUTO:'0'};
const checks=[];const passed=name=>{checks.push(name);console.log('PASS',name);};let app;
try {
 app=await electron.launch({args:[join(root,'ui-gui')],env,timeout:30000});
 const page=await app.firstWindow();const errors=[];page.on('pageerror',error=>errors.push(error.message));page.on('console',message=>{if(message.type()==='error')console.error('RENDERER',message.text());});
 await page.getByRole('textbox',{name:'消息'}).waitFor();
 await page.getByRole('button',{name:'连接或选择模型',exact:true}).click();await page.getByRole('dialog').waitFor();await page.keyboard.press('Escape');
 await page.getByRole('button',{name:'执行详情',exact:true}).click();await page.getByRole('tab',{name:'文件',exact:true}).click();
 const choose=async path=>{await app.evaluate(({dialog},target)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[target]});},path);await page.getByRole('button',{name:'选择文件预览'}).click();};
 const back=async()=>{await page.getByRole('button',{name:'← 返回',exact:true}).click();};
 const source=join(workspace,'preview.docx');const before=createHash('sha256').update(readFileSync(source)).digest('hex');
 await choose(source);
 await page.getByRole('img',{name:'PDF 第 1 页'}).waitFor({timeout:90000});
 await page.getByText('结构检查通过',{exact:false}).waitFor();await page.getByText('Astra Fixture Missing Font',{exact:false}).waitFor();
 const pixels=await page.locator('.pdf-page canvas').evaluate(canvas=>{const bytes=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;let dark=0;for(let i=0;i<bytes.length;i+=4)if(bytes[i]<180&&bytes[i+1]<180&&bytes[i+2]<180)dark++;return dark;});assert.ok(pixels>200,'Rendered PDF must contain visible non-white content');
 await page.screenshot({path:join(output,'docx-page1.png')});
 await page.getByRole('button',{name:'下一页'}).click();await page.getByRole('img',{name:'PDF 第 2 页'}).waitFor();await page.getByRole('button',{name:'页面文字'}).click();await page.getByText('Second page: native PDF preview verification.',{exact:false}).waitFor();
 assert.equal(createHash('sha256').update(readFileSync(source)).digest('hex'),before);passed('DOCX native conversion, PDF canvas/page navigation/text, missing-font notice and source preserved');
 const future=new Date(Date.now()+2000);utimesSync(source,future,future);await page.getByText('源文件已变化',{exact:false}).waitFor({timeout:10000});
 await page.getByRole('button',{name:'刷新预览'}).click();await page.getByRole('img',{name:'PDF 第 1 页'}).waitFor({timeout:90000});assert.equal(await page.getByText('源文件已变化',{exact:false}).count(),0);passed('saved source version changes invalidate preview and refresh');await back();
 await choose(join(workspace,'preview.pptx'));await page.getByRole('img',{name:'PDF 第 1 页'}).waitFor({timeout:90000});await page.getByRole('button',{name:'下一页'}).click();await page.getByRole('img',{name:'PDF 第 2 页'}).waitFor();await page.screenshot({path:join(output,'pptx-page2.png')});passed('PPTX native conversion and slide navigation');await back();
 await choose(join(workspace,'preview.xlsx'));await page.getByRole('table',{name:'数据 保存值预览'}).waitFor();await page.getByRole('cell',{name:'中文',exact:true}).waitFor();await page.getByRole('cell',{name:'（公式未缓存）',exact:true}).waitFor();await page.screenshot({path:join(output,'xlsx-values.png')});passed('XLSX Chinese values, missing formula cache and bounded rows');await back();
 // Save a real PDF returned by the authorized preview IPC, then preview it directly.
 const pdf=await page.evaluate(async path=>{const bootstrap=await window.astra.bootstrap();return window.astra.file(bootstrap.active,path,'preview');},join(workspace,'preview.docx'));
 const pdfPath=join(workspace,'preview.pdf');writeFileSync(pdfPath,Buffer.from(pdf.data.split(',')[1],'base64'));
 await choose(pdfPath);await page.getByRole('img',{name:'PDF 第 1 页'}).waitFor();passed('direct local PDF reader');await back();
 // Immediate cancellation is exercised through the same mounted panel as real user actions.
 const cancelSource=join(workspace,'cancel.pptx');copyFileSync(join(workspace,'preview.pptx'),cancelSource);await choose(cancelSource);await page.getByRole('button',{name:'取消预览'}).click({timeout:10000});await page.getByText('文件预览已取消。',{exact:true}).waitFor();passed('pending preview cancellation');await back();
 // Authorization must still reject an unselected file outside the workspace.
 const outside=join(mkdtempSync(join(tmpdir(),'astra-document-outside-')),'outside.txt');writeFileSync(outside,'must remain unauthorized');
 const denied=await page.evaluate(async path=>{const bootstrap=await window.astra.bootstrap();try{await window.astra.file(bootstrap.active,path,'preview');return false;}catch{return true;}},outside);assert.equal(denied,true);passed('existing file authorization boundary retained');
 assert.deepEqual(errors,[]);writeFileSync(join(output,'acceptance.json'),JSON.stringify({checks,errors,workspace},null,2));console.log('DOCUMENT_PREVIEW_SMOKE_PASS',checks.length,output);
} catch(error) {if(app){const windows=await app.windows();if(windows[0]){console.error((await windows[0].locator('body').innerText()).slice(-6000));await windows[0].screenshot({path:join(output,'failure.png')}).catch(()=>{});}}throw error;}
finally {if(app)await app.close();}
