import React, { useEffect, useRef, useState } from 'react';
import { getDocument, GlobalWorkerOptions, type PDFDocumentProxy, type RenderTask } from 'pdfjs-dist';
import workerURL from 'pdfjs-dist/build/pdf.worker.mjs?url';

GlobalWorkerOptions.workerSrc=workerURL;
/** PDF bytes remain local. No JavaScript, actions, attachments, or external link handlers execute. */
export default function PDFPreview({data}:{data:string}) {
  const container=useRef<HTMLDivElement>(null);const pageHost=useRef<HTMLDivElement>(null);
  const [pdf,setPdf]=useState<PDFDocumentProxy>();const [page,setPage]=useState(1);const [width,setWidth]=useState(400);
  const [error,setError]=useState('');const [busy,setBusy]=useState(true);const [text,setText]=useState('');const [showText,setShowText]=useState(false);
  useEffect(()=>{
    let alive=true;setPdf(undefined);setPage(1);setError('');setBusy(true);
    const bytes=Uint8Array.from(atob(data.slice(data.indexOf(',')+1)),char=>char.charCodeAt(0));
    const assets=new URL('pdfjs/',document.baseURI);
    const loading=getDocument({data:bytes,cMapUrl:new URL('cmaps/',assets).href,cMapPacked:true,standardFontDataUrl:new URL('standard_fonts/',assets).href,wasmUrl:new URL('wasm/',assets).href,useSystemFonts:true,stopAtErrors:true});
    void loading.promise.then(document=>{if(alive)setPdf(document);}).catch(cause=>{if(alive){setError(`PDF 无法打开：${cause.message||cause}`);setBusy(false);}});
    return()=>{alive=false;void loading.destroy();};
  },[data]);
  useEffect(()=>{const node=container.current;if(!node)return;const observer=new ResizeObserver(entries=>setWidth(Math.max(160,Math.floor(entries[0].contentRect.width))));observer.observe(node);return()=>observer.disconnect();},[]);
  useEffect(()=>{
    if(!pdf)return;let alive=true;let render:RenderTask|undefined;setBusy(true);setError('');setText('');pageHost.current?.replaceChildren();
    void (async()=>{
      const documentPage=await pdf.getPage(page);if(!alive)return;
      const original=documentPage.getViewport({scale:1});
      const scale=Math.min(width/original.width,4096/original.height);const viewport=documentPage.getViewport({scale});const pixelRatio=Math.min(devicePixelRatio||1,2);
      const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.floor(viewport.width*pixelRatio));canvas.height=Math.max(1,Math.floor(viewport.height*pixelRatio));canvas.style.width=`${viewport.width}px`;canvas.style.height=`${viewport.height}px`;canvas.setAttribute('aria-label',`PDF 第 ${page} 页`);canvas.setAttribute('role','img');
      render=documentPage.render({canvas,viewport,transform:pixelRatio!==1?[pixelRatio,0,0,pixelRatio,0,0]:undefined});
      await render.promise;if(!alive)return;pageHost.current?.replaceChildren(canvas);setBusy(false);
      const content=await documentPage.getTextContent();if(alive)setText(content.items.map(item=>'str' in item?item.str+('hasEOL' in item&&item.hasEOL?'\n':' '):'').join(''));
    })().catch(cause=>{if(alive && cause?.name!=='RenderingCancelledException'){setError(`PDF 页面无法显示：${cause.message||cause}`);setBusy(false);}});
    return()=>{alive=false;render?.cancel();};
  },[pdf,page,width]);
  return <div ref={container} className="pdf-preview">
    <div className="actions"><button disabled={!pdf||page<=1} onClick={()=>setPage(value=>value-1)}>上一页</button><span>{pdf?`${page} / ${pdf.numPages}`:'PDF'}</span><button disabled={!pdf||page>=pdf.numPages} onClick={()=>setPage(value=>value+1)}>下一页</button><button disabled={!text} onClick={()=>setShowText(value=>!value)}>{showText?'页面图像':'页面文字'}</button></div>
    {busy&&<p role="status">正在显示 PDF…</p>}{error&&<p className="error-text" role="alert">{error}</p>}
    <div ref={pageHost} className="pdf-page" hidden={showText}/>{showText&&<pre className="file-preview">{text}</pre>}
  </div>;
}
