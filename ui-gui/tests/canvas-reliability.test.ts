import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { readCanvas, recordVersion, listVersions } from "../src/main/canvas-files.js";

test("the asset budget bounds actual reads before inline conversion", async () => {
  const io = fs.promises as any;
  const root = await io.realpath(await io.mkdtemp(join(tmpdir(), "canvas-budget-")));
  const folder = join(root, "deck.canvas"), assets = join(folder, "assets");
  const originalRead = io.readFile, originalOpen = io.open;
  let readBytes = 0;
  try {
    await io.mkdir(assets, {recursive:true});
    await io.writeFile(join(folder,"canvas.json"), JSON.stringify({type:"slides"}));
    const refs = [];
    for (let i=0;i<6;i++) {
      await io.writeFile(join(assets, `${i}.png`), Buffer.alloc(8*1024*1024));
      refs.push(`<img src="assets/${i}.png">`);
    }
    await io.writeFile(join(folder,"slides.html"), `<section class="slide">${refs.join("")}</section>`);
    io.readFile = async (path: any, ...args: any[]) => {
      const value = await originalRead(path, ...args);
      if (String(path).startsWith(assets+"/")) readBytes += value.byteLength;
      return value;
    };
    io.open = async (path: any, ...args: any[]) => {
      const handle = await originalOpen(path, ...args);
      if (String(path).startsWith(assets+"/")) {
        const read = handle.read.bind(handle);
        handle.read = async (...params: any[]) => {
          const result = await read(...params);
          readBytes += result.bytesRead;
          return result;
        };
      }
      return handle;
    };
    syncBuiltinESMExports();
    const deck = await readCanvas(join(folder,"canvas.json"), "view");
    assert.equal((deck.inlined.match(/data:image/g) ?? []).length, 5);
    assert.deepEqual(deck.missing, ["5.png"]);
    assert.ok(readBytes <= 40*1024*1024, `read ${readBytes} bytes before applying the budget`);
  } finally {
    io.readFile = originalRead; io.open = originalOpen; syncBuiltinESMExports();
    await io.rm(root, {recursive:true,force:true});
  }
});

test("concurrent distinct snapshots reserve different version IDs", async () => {
  const io = fs.promises as any;
  const root = await io.realpath(await io.mkdtemp(join(tmpdir(), "canvas-race-")));
  const folder=join(root,"doc.canvas"), target=join(folder,"doc.md"), manifest=join(folder,"canvas.json");
  await io.mkdir(join(folder,".versions"),{recursive:true});
  await io.writeFile(manifest,JSON.stringify({type:"doc"}));
  await io.writeFile(target,"VERSION_A");
  const originalRead=io.readFile, originalMkdir=io.mkdir;
  let notifyA!:()=>void, releaseA!:()=>void, releaseBoth!:()=>void, arrivals=0;
  const aRead=new Promise<void>(r=>notifyA=r), resumeA=new Promise<void>(r=>releaseA=r);
  const both=new Promise<void>(r=>releaseBoth=r);
  io.readFile=async(path:any,...args:any[])=>{
    const data=await originalRead(path,...args);
    if(String(path)===target) {
      if(data.toString()==="VERSION_A") {notifyA();await resumeA;} else releaseA();
    }
    return data;
  };
  io.mkdir=async(path:any,...args:any[])=>{
    if(String(path)===join(folder,".versions","0001")) {
      if(++arrivals===2) releaseBoth();
      await both;
    }
    return originalMkdir(path,...args);
  };
  syncBuiltinESMExports();
  try {
    const first=recordVersion(manifest,"writer-A");
    await aRead; await io.writeFile(target,"VERSION_B");
    const ids=await Promise.all([first,recordVersion(manifest,"writer-B")]);
    assert.equal(new Set(ids).size,2);
    const versions=await listVersions(manifest);
    assert.equal(versions.length,2);
    const contents=await Promise.all(versions.map(v=>originalRead(join(folder,".versions",v.id,"doc.md"),"utf8")));
    assert.deepEqual(contents.sort(),["VERSION_A","VERSION_B"]);
  } finally {
    io.readFile=originalRead;io.mkdir=originalMkdir;syncBuiltinESMExports();
    await io.rm(root,{recursive:true,force:true});
  }
});
