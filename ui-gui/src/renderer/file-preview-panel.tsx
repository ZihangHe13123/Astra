import React, { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { Camera, ExternalLink, FolderOpen, FileCode, Eye, History, Maximize2, MessageSquarePlus, Minimize2 } from 'lucide-react';
import type { CanvasVersion, FilePreview } from '../file-preview-types.js';
import { dirname, isMarkdownPath, openComments, outline } from './doc-preview.js';
import { Markdown } from './messages.js';
import { PageFrame } from './page-frame.js';

const PDFPreview = lazy(()=>import('./pdf-preview.js'));
type Props = {runtime:string; path:string; serial:number; dismiss:()=>void; fail:(error:unknown)=>void; wide?:boolean; onWide?:(wide:boolean)=>void; onAttach?:(paths:string[])=>void};

/** Each mounted selection owns its request. Dismissal/session changes cancel the host conversion. */
export function FilePreviewPanel({runtime,path,serial,dismiss,fail,wide,onWide,onAttach}: Props) {
  const [preview,setPreview]=useState<FilePreview>();
  const [error,setError]=useState(''); const [loading,setLoading]=useState(true);
  const [revision,setRevision]=useState(0); const [stale,setStale]=useState('');
  const [docView,setDocView]=useState<'rendered'|'source'>('rendered');
  const [picturing,setPicturing]=useState(false);
  // A canvas's kept versions (the list is open while set), the one being looked at, and the person's comment on a passage.
  const [versions,setVersions]=useState<CanvasVersion[]>();
  const [viewing,setViewing]=useState<{id:string; preview:FilePreview}>();
  const [passage,setPassage]=useState(''); const [note,setNote]=useState('');
  const [busy,setBusy]=useState(false); const [notice,setNotice]=useState('');
  const pending=useRef<{id:string; cancelled:boolean}>();
  const body=useRef<HTMLDivElement>(null);
  useEffect(()=>{
    let alive=true; const request={id:crypto.randomUUID(),cancelled:false}; pending.current=request;
    setLoading(true); setError(''); setPreview(undefined); setStale(''); setViewing(undefined);
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
  const canvas=preview?.canvas;
  const markdown=preview?.text!==undefined && (isMarkdownPath(path) || canvas?.type==='doc');
  const page=preview?.kind==='page';
  useEffect(()=>{setNotice('');setVersions(undefined);setPassage('');},[path,serial]);
  const loadVersions=()=>window.astra.canvas(runtime,path,'versions').then(value=>setVersions([...value.versions||[]].reverse())).catch(fail);
  // A change while the list is open shows in it.
  useEffect(()=>{if(versions && preview)void loadVersions();},[preview]);
  const view=(id:string)=>{void window.astra.canvas(runtime,path,'preview',id).then(value=>{if(value.preview)setViewing({id,preview:value.preview});}).catch(fail);};
  const change=(work:Promise<unknown>,done:string)=>{setBusy(true);void work.then(()=>{setViewing(undefined);setPassage('');setNote('');setNotice(done);setRevision(value=>value+1);}).catch(fail).finally(()=>setBusy(false));};
  const restore=(id:string)=>change(window.astra.canvas(runtime,path,'restore',id),`已恢复成版本 ${Number(id)}；恢复前的内容也留了一个版本。`);
  const comment=()=>change(window.astra.canvas(runtime,path,'comment',{passage,comment:note}),'批注已写进文档。在对话里让 Astra 处理批注即可。');
  // What the person selects in a document canvas is the passage a comment is about.
  const pick=()=>{
    if(canvas?.type!=='doc' || viewing || docView!=='rendered')return;
    const selection=window.getSelection(); const text=selection?.toString().split(/\s+/).filter(Boolean).join(' ')||'';
    if(text && selection?.anchorNode && body.current?.contains(selection.anchorNode))setPassage(text.slice(0,1000));
  };
  // The picture is of the saved page, drawn afresh; it lands in the message being written, which the enlarged panel covers.
  const picture=()=>{setPicturing(true);void window.astra.file(runtime,path,'snapshot').then(value=>{if(value.path){onAttach?.([value.path]);onWide?.(false);}}).catch(fail).finally(()=>setPicturing(false));};
  // Only a page takes the whole window; anything else shown next gives the room back.
  useEffect(()=>{ if(wide && preview && !page) onWide?.(false); },[wide,preview,page]);
  return <section className="file-preview-panel" aria-label="文件预览">
    <button className="text-button" onClick={dismiss}>← 返回</button><h3>{canvas?.title || path.split(/[\\/]/).pop()}</h3>
    {canvas && <p className="muted small">{canvas.type==='doc' ? '文档 · 选中一段文字可以批注' : `幻灯片 · ${canvas.slides} 页 · 点任一页放映，Esc 退出`}</p>}
    <div className="actions">
      {(markdown || page) && <button onClick={()=>setDocView(docView==='rendered'?'source':'rendered')}>{docView==='rendered'?<><FileCode size={14}/> 源码</>:<><Eye size={14}/> 预览</>}</button>}
      {page && onWide && <button aria-pressed={!!wide} onClick={()=>onWide(!wide)}>{wide?<><Minimize2 size={14}/> 还原</>:<><Maximize2 size={14}/> 放大</>}</button>}
      {canvas && <button aria-pressed={!!versions} onClick={()=>{if(versions){setVersions(undefined);setViewing(undefined);}else void loadVersions();}}><History size={14}/> 版本</button>}
      {page && onAttach && <button onClick={picture} disabled={picturing} title="把这个页面渲染成图片，附到正在写的消息里">{picturing?'正在截图…':<><Camera size={14}/> 发给 Astra 看</>}</button>}
      <button onClick={()=>setRevision(value=>value+1)} disabled={loading}>刷新预览</button>
      <button onClick={()=>{void window.astra.file(runtime,path,'open').catch(fail);}}><ExternalLink size={14}/> 系统打开</button>
      <button onClick={()=>{void window.astra.file(runtime,path,'reveal').catch(fail);}}><FolderOpen size={14}/> 定位</button>
    </div>
    {loading && <div role="status"><p>正在读取或转换文档…</p><button onClick={cancel}>取消预览</button></div>}
    {error && <p role="alert" className="error-text">{error}</p>}
    {stale && <p role="status" className="preview-warning">{stale}</p>}
    {notice && <p role="status" className="muted small canvas-notice">{notice}</p>}
    {versions && <CanvasVersions versions={versions} viewing={viewing?.id} view={view}/>}
    {viewing && <div className="preview-warning canvas-viewing" role="status"><span>正在看版本 {Number(viewing.id)}，不是现在的内容。</span>
      <button onClick={()=>restore(viewing.id)} disabled={busy}>恢复成这个版本</button><button onClick={()=>setViewing(undefined)}>回到现在</button></div>}
    {preview && <div ref={body} onMouseUp={pick}><DocumentPreviewBody preview={viewing?.preview ?? preview} runtime={runtime} fail={fail} docView={docView}/></div>}
    {passage && <form className="canvas-comment" onSubmit={event=>{event.preventDefault();if(note.trim())comment();}}>
      <p className="small"><MessageSquarePlus size={14}/> 批注「{passage.length>60?`${passage.slice(0,60)}…`:passage}」</p>
      <textarea autoFocus rows={3} value={note} maxLength={500} placeholder="想让 Astra 怎么改这段？" onChange={event=>setNote(event.target.value)}
        onKeyDown={event=>{if(event.key==='Enter' && (event.metaKey || event.ctrlKey)){event.preventDefault();if(note.trim())comment();}}}/>
      <div className="actions"><button type="submit" disabled={busy || !note.trim()}>写进文档</button><button type="button" onClick={()=>{setPassage('');setNote('');}}>取消</button></div>
    </form>}
  </section>;
}
const VERSION_SOURCES: Record<string,string> = {create:'创建', write:'Astra 修改', comment:'批注', restore:'恢复'};
function versionTime(seconds:number):string {
  const at=new Date(seconds*1000), now=new Date(); const pad=(value:number)=>String(value).padStart(2,'0');
  const time=`${pad(at.getHours())}:${pad(at.getMinutes())}`;
  return at.toDateString()===now.toDateString() ? time : `${at.getMonth()+1}-${at.getDate()} ${time}`;
}
/** The kept versions, newest first; choosing one shows it in place of the current content. */
function CanvasVersions({versions,viewing,view}:{versions:CanvasVersion[];viewing?:string;view:(id:string)=>void}) {
  if(!versions.length)return <p className="muted small">还没有留下版本：画布被改动后会自动留一个。</p>;
  return <ol className="canvas-versions" aria-label="画布版本">{versions.map((version,index)=><li key={version.id}>
    <button aria-current={viewing===version.id || undefined} onClick={()=>view(version.id)}>
      <b>版本 {Number(version.id)}</b><span>{VERSION_SOURCES[version.source] ?? version.source}</span><span className="muted">{versionTime(version.at)}{index===0?' · 最新':''}</span>
    </button></li>)}</ol>;
}
/** A document's headings; choosing one scrolls to it in the rendered document. */
function DocOutline({headings,scroll}:{headings:{level:number;text:string}[];scroll:(index:number)=>void}) {
  if(headings.length<2)return null;
  const top=Math.min(...headings.map(heading=>heading.level));
  return <nav className="doc-outline" aria-label="大纲"><ol>{headings.map((heading,index)=><li key={index} style={{paddingLeft:`${(heading.level-top)*12}px`}}>
    <button className="text-button" onClick={()=>scroll(index)}>{heading.text}</button></li>)}</ol></nav>;
}
export function DocumentPreviewBody({preview,runtime,fail,docView='rendered'}: {preview:FilePreview;runtime:string;fail:(error:unknown)=>void;docView?:'rendered'|'source'}) {
  const doc=preview.canvas?.type==='doc';
  const markdown=preview.text!==undefined && (doc || isMarkdownPath(preview.path||''));
  const comments=markdown?openComments(preview.text||''):[];
  const rendered=useRef<HTMLDivElement>(null);
  const headings=doc?outline(preview.text||''):[];
  // The heading with this text, counting the ones before it that read the same; else the one at its place.
  const scroll=(index:number)=>{
    const shown=[...rendered.current?.querySelectorAll('h1,h2,h3,h4,h5,h6')||[]];
    const same=headings.slice(0,index).filter(heading=>heading.text===headings[index].text).length;
    const target=shown.filter(element=>(element.textContent||'').split(/\s+/).filter(Boolean).join(' ')===headings[index].text)[same] ?? shown[index];
    target?.scrollIntoView({block:'start',behavior:'smooth'});
  };
  return <>
    {preview.checks && <p className="muted small">结构检查通过 · 已重新打开文档包（{preview.checks.parts} 个条目）；未进行版式验收。</p>}
    {preview.converted && <p className="muted small">已在本机转换为 PDF{preview.cached?' · 使用当前源文件版本的缓存':''}。</p>}
    {!!preview.missingFonts?.length && <p className="preview-warning" role="status">缺少字体：{preview.missingFonts.join('、')}。替代字体可能改变分页。</p>}
    {([...preview.checks?.warnings||[], ...preview.warnings||[]].filter((value,index,all)=>all.indexOf(value)===index)).map((warning,index)=><p className="preview-warning small" key={index}>{warning}</p>)}
    {preview.kind==='pdf' && preview.data ? <Suspense fallback={<p role="status">正在加载 PDF 阅读器…</p>}><PDFPreview data={preview.data}/></Suspense>
      : preview.kind==='spreadsheet' ? <SpreadsheetPreview sheets={preview.sheets||[]}/>
      : preview.data ? <img className="preview-image" src={preview.data} alt="文件预览"/>
      : preview.kind==='page' && docView==='rendered' ? <PageFrame source={preview.text||''}/>
      : markdown && docView==='rendered' ? <>
        {comments.length>0 && <section className="summary-card doc-comments"><h4>待处理评论 <span className="count">{comments.length}</span></h4>{comments.map((comment,index)=><p key={index} className="small">{comment}</p>)}</section>}
        {doc && <DocOutline headings={headings} scroll={scroll}/>}
        <div className="doc-preview" ref={rendered}><Markdown text={preview.text||''} runtime={runtime} base={dirname(preview.path||'')} fail={fail}/></div>
      </> : preview.canvas ? <pre className="file-preview">{preview.canvas.source}</pre>
      : <pre className="file-preview">{preview.text}</pre>}
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
