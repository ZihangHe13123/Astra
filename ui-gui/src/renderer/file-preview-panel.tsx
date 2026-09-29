import React, { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { ExternalLink, FolderOpen, FileCode, Eye } from 'lucide-react';
import type { FilePreview } from '../file-preview-types.js';
import { dirname, isMarkdownPath, openComments } from './doc-preview.js';
import { Markdown } from './messages.js';

const PDFPreview = lazy(()=>import('./pdf-preview.js'));
type Props = {runtime:string; path:string; serial:number; dismiss:()=>void; fail:(error:unknown)=>void};

/** Each mounted selection owns its request. Dismissal/session changes cancel the host conversion. */
export function FilePreviewPanel({runtime,path,serial,dismiss,fail}: Props) {
  const [preview,setPreview]=useState<FilePreview>();
  const [error,setError]=useState(''); const [loading,setLoading]=useState(true);
  const [revision,setRevision]=useState(0); const [stale,setStale]=useState('');
  const [docView,setDocView]=useState<'rendered'|'source'>('rendered');
  const pending=useRef<{id:string; cancelled:boolean}>();
  useEffect(()=>{
    let alive=true; const request={id:crypto.randomUUID(),cancelled:false}; pending.current=request;
    setLoading(true); setError(''); setPreview(undefined); setStale('');
    void window.astra.file(runtime,path,'preview',request.id).then(value=>{
      if(alive && !request.cancelled) {setPreview(value);setLoading(false);}
    }).catch(cause=>{if(alive && !request.cancelled){setError(String(cause instanceof Error ? cause.message : cause));setLoading(false);}});
    return ()=>{alive=false;request.cancelled=true;if(pending.current===request)pending.current=undefined;void window.astra.cancelPreview(runtime,request.id).catch(()=>{});};
  },[runtime,path,serial,revision]);
  useEffect(()=>{
    if(!preview?.sourceVersion)return;
    let alive=true, checking=false;
    const check=()=>{if(checking || document.hidden)return;checking=true;void window.astra.file(runtime,path,'version').then(value=>{
      if(alive)setStale(value.sourceVersion!==preview.sourceVersion ? '源文件已变化，当前显示上一次保存版本的预览。请刷新。' : '');
    }).catch(()=>{if(alive)setStale('暂时无法核对源文件，当前显示已读取的版本。');}).finally(()=>{checking=false;});};
    const interval=setInterval(check,3000);window.addEventListener('focus',check);
    return()=>{alive=false;clearInterval(interval);window.removeEventListener('focus',check);};
  },[runtime,path,preview?.sourceVersion]);
  const cancel=()=>{const request=pending.current;if(request){request.cancelled=true;void window.astra.cancelPreview(runtime,request.id).catch(fail);}setLoading(false);setError('文件预览已取消。');};
  const markdown=preview?.text!==undefined && isMarkdownPath(path);
  return <section className="file-preview-panel" aria-label="文件预览">
    <button className="text-button" onClick={dismiss}>← 返回</button><h3>{path.split(/[\\/]/).pop()}</h3>
    <div className="actions">
      {markdown && <button onClick={()=>setDocView(docView==='rendered'?'source':'rendered')}>{docView==='rendered'?<><FileCode size={14}/> 源码</>:<><Eye size={14}/> 预览</>}</button>}
      <button onClick={()=>setRevision(value=>value+1)} disabled={loading}>刷新预览</button>
      <button onClick={()=>{void window.astra.file(runtime,path,'open').catch(fail);}}><ExternalLink size={14}/> 系统打开</button>
      <button onClick={()=>{void window.astra.file(runtime,path,'reveal').catch(fail);}}><FolderOpen size={14}/> 定位</button>
    </div>
    {loading && <div role="status"><p>正在读取或转换文档…</p><button onClick={cancel}>取消预览</button></div>}
    {error && <p role="alert" className="error-text">{error}</p>}
    {stale && <p role="status" className="preview-warning">{stale}</p>}
    {preview && <DocumentPreviewBody preview={preview} runtime={runtime} fail={fail} docView={docView}/>}
  </section>;
}
export function DocumentPreviewBody({preview,runtime,fail,docView='rendered'}: {preview:FilePreview;runtime:string;fail:(error:unknown)=>void;docView?:'rendered'|'source'}) {
  const markdown=preview.text!==undefined && isMarkdownPath(preview.path||'');
  const comments=markdown?openComments(preview.text||''):[];
  return <>
    {preview.checks && <p className="muted small">结构检查通过 · 已重新打开文档包（{preview.checks.parts} 个条目）；未进行版式验收。</p>}
    {preview.converted && <p className="muted small">已在本机转换为 PDF{preview.cached?' · 使用当前源文件版本的缓存':''}。</p>}
    {!!preview.missingFonts?.length && <p className="preview-warning" role="status">缺少字体：{preview.missingFonts.join('、')}。替代字体可能改变分页。</p>}
    {([...preview.checks?.warnings||[], ...preview.warnings||[]].filter((value,index,all)=>all.indexOf(value)===index)).map((warning,index)=><p className="preview-warning small" key={index}>{warning}</p>)}
    {preview.kind==='pdf' && preview.data ? <Suspense fallback={<p role="status">正在加载 PDF 阅读器…</p>}><PDFPreview data={preview.data}/></Suspense>
      : preview.kind==='spreadsheet' ? <SpreadsheetPreview sheets={preview.sheets||[]}/>
      : preview.data ? <img className="preview-image" src={preview.data} alt="文件预览"/>
      : markdown && docView==='rendered' ? <>
        {comments.length>0 && <section className="summary-card doc-comments"><h4>待处理评论 <span className="count">{comments.length}</span></h4>{comments.map((comment,index)=><p key={index} className="small">{comment}</p>)}</section>}
        <div className="doc-preview"><Markdown text={preview.text||''} runtime={runtime} base={dirname(preview.path||'')} fail={fail}/></div>
      </> : <pre className="file-preview">{preview.text}</pre>}
    {preview.truncated && <p className="muted">仅预览前 256 KiB，原文件保持完整。</p>}
  </>;
}
function columnName(index:number):string {let value=index+1,name='';while(value){value--;name=String.fromCharCode(65+value%26)+name;value=Math.floor(value/26);}return name;}
function SpreadsheetPreview({sheets}:{sheets:NonNullable<FilePreview['sheets']>}) {
  const [selected,setSelected]=useState(0);const sheet=sheets[Math.min(selected,sheets.length-1)];
  if(!sheet)return <p className="muted">没有可预览的工作表。</p>;
  const columns=Math.max(1,...sheet.rows.map(row=>row.length));
  return <div className="spreadsheet-preview">
    <label>工作表 <select value={selected} onChange={event=>setSelected(Number(event.target.value))}>{sheets.map((item,index)=><option value={index} key={index}>{item.name}</option>)}</select></label>
    <div className="spreadsheet-scroll"><table aria-label={`${sheet.name} 保存值预览`}><thead><tr><th scope="col">行</th>{Array.from({length:columns},(_,index)=><th scope="col" key={index}>{columnName(index)}</th>)}</tr></thead><tbody>{sheet.rows.map((row,index)=><tr key={index}><th scope="row">{index+1}</th>{Array.from({length:columns},(_,column)=><td key={column}>{row[column]||''}</td>)}</tr>)}</tbody></table></div>
    {sheet.truncated && <p className="muted small">仅显示前 200 行、50 列的保存值。</p>}
  </div>;
}
