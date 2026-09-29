import { posix } from 'node:path';
import { strFromU8, unzipSync } from 'fflate';
import { XMLParser, XMLValidator } from 'fast-xml-parser';
import type { DocumentCheck, SpreadsheetSheet } from '../file-preview-types.js';

const MAX_ENTRIES = 5000;
const MAX_EXPANDED_BYTES = 64 * 1024 * 1024;
const MAX_XML_BYTES = 16 * 1024 * 1024;
const MAX_ROWS = 200, MAX_COLUMNS = 50, MAX_SHEETS = 12;
const list = <T>(value: T | T[] | undefined): T[] => value === undefined ? [] : Array.isArray(value) ? value : [value];
const parser = new XMLParser({ignoreAttributes:false, removeNSPrefix:true, parseTagValue:false, parseAttributeValue:false, processEntities:true, htmlEntities:true});
const mainParts = {docx:'word/document.xml', pptx:'ppt/presentation.xml', xlsx:'xl/workbook.xml'} as const;
type Format = keyof typeof mainParts;
type Package = {xml: Record<string, any>; names: Set<string>; count: number; external: number};

/** Opens ZIP entries in memory only; never extracts paths or invokes an Office application. */
function reopen(bytes: Uint8Array, format: Format): Package {
  validateOfficeArchive(bytes);
  let count = 0, expanded = 0;
  const names = new Set<string>();
  let entries: Record<string, Uint8Array>;
  try {
    entries = unzipSync(bytes, {filter:entry => {
      count++; expanded += entry.originalSize;
      if (count > MAX_ENTRIES || expanded > MAX_EXPANDED_BYTES) throw new Error('Office ZIP 解压大小或条目数超过预览限制。');
      if (names.has(entry.name) || entry.name.includes('\\') || entry.name.startsWith('/') || entry.name.split('/').includes('..')) throw new Error('Office ZIP 包含重复或无效的文件路径。');
      names.add(entry.name);
      const xml = /\.(xml|rels)$/i.test(entry.name);
      if (xml && entry.originalSize > MAX_XML_BYTES) throw new Error('Office XML 解压大小超过预览限制。');
      return xml;
    }});
  } catch (error) { throw new Error(`无法重新打开 Office 文件：${error instanceof Error ? error.message : String(error)}`); }
  for (const required of ['[Content_Types].xml', mainParts[format], '_rels/.rels']) if (!entries[required]) throw new Error(`Office 结构检查失败，缺少 ${required}。`);
  const xml: Record<string, any> = Object.create(null);
  for (const [name, data] of Object.entries(entries)) {
    const text = strFromU8(data);
    if (/<!\s*(?:DOCTYPE|ENTITY)\b/i.test(text)) throw new Error(`Office XML 不接受 DTD 或实体声明：${name}。`);
    if (XMLValidator.validate(text) !== true) throw new Error(`Office XML 无法重新解析：${name}。`);
    try { xml[name] = parser.parse(text); } catch { throw new Error(`Office XML 无法重新解析：${name}。`); }
  }
  if (!xml['[Content_Types].xml'].Types) throw new Error('Office Content Types 结构无效。');
  const expectedRoot = {docx:'document', pptx:'presentation', xlsx:'workbook'}[format];
  if (!xml[mainParts[format]][expectedRoot]) throw new Error(`Office 主文档结构无效：${mainParts[format]}。`);
  let external = 0;
  for (const [name, data] of Object.entries(xml)) {
    if (!name.endsWith('.rels')) continue;
    if (!data.Relationships) throw new Error(`Office 关系文件无效：${name}。`);
    for (const relationship of list<any>(data.Relationships.Relationship)) {
      if (relationship['@_TargetMode'] === 'External') { external++; continue; }
      const target = relationTarget(name, relationship['@_Target']);
      if (!names.has(target)) throw new Error(`Office 引用缺失：${name} → ${target}。`);
    }
  }
  const roots = list<any>(xml['_rels/.rels'].Relationships.Relationship);
  if (!roots.some(rel=>rel['@_TargetMode'] !== 'External' && relationTarget('_rels/.rels',rel['@_Target']) === mainParts[format])) throw new Error('Office 包关系未指向主文档。');
  return {xml, names, count, external};
}
function relationTarget(rels: string, raw: unknown): string {
  if (typeof raw !== 'string' || !raw) throw new Error('Office 关系缺少目标。');
  let target = raw.split('#')[0];
  try { target = decodeURIComponent(target); } catch { throw new Error('Office 关系目标编码无效。'); }
  const base = rels === '_rels/.rels' ? '' : posix.dirname(posix.dirname(rels));
  const normalized = target.startsWith('/') ? posix.normalize(target.slice(1)) : posix.normalize(posix.join(base, target));
  if (!normalized || normalized.startsWith('../') || normalized.includes('\\') || /^[a-z][a-z\d+.-]*:/i.test(normalized)) throw new Error('Office 关系目标超出文档包。');
  return normalized;
}
function report(pkg: Package, format: Format): DocumentCheck { return {format, status:'passed', parts:pkg.count, warnings:pkg.external ? [`文档含 ${pkg.external} 个外部引用；结构检查未访问这些目标。`] : []}; }
/** ZIP/XML reopen + required parts + internal relationship targets. This does not prove page layout. */
export function inspectOffice(bytes: Uint8Array, format: Format): DocumentCheck { return report(reopen(bytes, format), format); }

function textContent(value: any): string {
  if (value === undefined || value === null) return '';
  if (typeof value !== 'object') return String(value);
  if (value.t !== undefined) return textContent(value.t);
  if (value.r !== undefined) return list(value.r).map(textContent).join('');
  return value['#text'] === undefined ? '' : String(value['#text']);
}
function columnIndex(reference: string): number {
  const letters = reference.match(/^[A-Z]+/i)?.[0]; if (!letters || letters.length > 3) return -1;
  return [...letters.toUpperCase()].reduce((value, char)=>value*26+char.charCodeAt(0)-64,0)-1;
}
/** A bounded grid of saved values. Formulas, links, macros and external data never execute. */
export function spreadsheetPreview(bytes: Uint8Array): {checks:DocumentCheck; sheets:SpreadsheetSheet[]; warnings:string[]} {
  const pkg = reopen(bytes,'xlsx'); const checks = report(pkg,'xlsx');
  const workbook = pkg.xml['xl/workbook.xml'].workbook;
  const relationships = list<any>(pkg.xml['xl/_rels/workbook.xml.rels']?.Relationships.Relationship);
  const shared = list<any>(pkg.xml['xl/sharedStrings.xml']?.sst.si).map(textContent);
  const allSheets = list<any>(workbook.sheets?.sheet);
  const warnings = ['显示保存的原始值或公式缓存值，未重新计算公式，未还原单元格样式；日期可能显示为序列数。', ...checks.warnings];
  if (allSheets.length > MAX_SHEETS) warnings.push(`仅显示前 ${MAX_SHEETS} 个工作表。`);
  let missingCache = false;
  const sheets = allSheets.slice(0,MAX_SHEETS).map((sheet): SpreadsheetSheet => {
    const relation = relationships.find(rel=>rel['@_Id'] === sheet['@_id']);
    if (!relation || relation['@_TargetMode'] === 'External') throw new Error('工作表关系缺失或指向外部文档。');
    const target = relationTarget('xl/_rels/workbook.xml.rels',relation['@_Target']);
    const worksheet = pkg.xml[target]?.worksheet;
    if (!worksheet) throw new Error(`工作表无法重新打开：${target}。`);
    const rows: string[][] = []; let truncated = false;
    for (const [index, row] of list<any>(worksheet.sheetData?.row).entries()) {
      const rowNumber = Number(row['@_r'] ?? index+1);
      if (!Number.isInteger(rowNumber) || rowNumber < 1) throw new Error('工作表行号无效。');
      if (rowNumber > MAX_ROWS) { truncated=true; continue; }
      const cells: string[] = [];
      for (const [cellIndex, cell] of list<any>(row.c).entries()) {
        const column = typeof cell['@_r'] === 'string' ? columnIndex(cell['@_r']) : cellIndex;
        if (column < 0) throw new Error('工作表单元格地址无效。');
        if (column >= MAX_COLUMNS) { truncated=true; continue; }
        let value = textContent(cell.v);
        if (cell.f !== undefined && (cell.v === undefined || (cell.v === '' && cell['@_t'] !== 'str'))) { value='（公式未缓存）'; missingCache=true; }
        else if (cell['@_t'] === 's' && value !== '') { const key = Number(value); if (!Number.isInteger(key) || key < 0 || key >= shared.length) throw new Error('工作表共享字符串引用缺失。'); value=shared[key]; }
        else if (cell['@_t'] === 'inlineStr') value=textContent(cell.is);
        else if (cell['@_t'] === 'b') value=value === '1' ? 'TRUE' : 'FALSE';
        while (cells.length < column) cells.push('');
        cells[column] = value.slice(0,4000);
      }
      // Preserve sparse positions so the table's row numbers stay meaningful.
      while (rows.length < rowNumber-1) rows.push([]);
      rows[rowNumber-1] = cells;
    }
    return {name:String(sheet['@_name'] ?? 'Sheet'), rows, truncated};
  });
  if (missingCache) warnings.push('部分公式未缓存结果，预览中已明确标记。');
  return {checks, sheets, warnings};
}

/** Reject ZIP64 and malformed offsets before any third-party ZIP decoder sees the archive. */
export function validateOfficeArchive(bytes: Uint8Array): void {
  const data=Buffer.from(bytes.buffer,bytes.byteOffset,bytes.byteLength);
  if(data.length<22 || data.length>32*1024*1024)throw new Error('Office ZIP 大小超出预览限制。');
  let end=data.length-22;
  while(end>=Math.max(0,data.length-65558) && data.readUInt32LE(end)!==0x06054b50)end--;
  if(end<0 || end<Math.max(0,data.length-65558) || end+22+data.readUInt16LE(end+20)!==data.length)throw new Error('Office ZIP 中央目录无效。');
  const count=data.readUInt16LE(end+10), offset=data.readUInt32LE(end+16), size=data.readUInt32LE(end+12);
  if(count===0xffff || offset===0xffffffff || size===0xffffffff || (end>=20 && data.readUInt32LE(end-20)===0x07064b50))throw new Error('Office 预览不支持 ZIP64 文档包。');
  if(data.readUInt16LE(end+4)!==0 || data.readUInt16LE(end+6)!==0 || data.readUInt16LE(end+8)!==count || count>MAX_ENTRIES || offset+size!==end)throw new Error('Office ZIP 中央目录无效或超过条目限制。');
  let position=offset;
  for(let index=0;index<count;index++) {
    if(position+46>end || data.readUInt32LE(position)!==0x02014b50)throw new Error('Office ZIP 中央目录损坏。');
    const compressed=data.readUInt32LE(position+20), expanded=data.readUInt32LE(position+24), local=data.readUInt32LE(position+42);
    if(compressed===0xffffffff || expanded===0xffffffff || local===0xffffffff)throw new Error('Office 预览不支持 ZIP64 文档包。');
    const nameLength=data.readUInt16LE(position+28), extraLength=data.readUInt16LE(position+30), commentLength=data.readUInt16LE(position+32);
    const next=position+46+nameLength+extraLength+commentLength;
    if(next>end || local+30>offset || data.readUInt32LE(local)!==0x04034b50)throw new Error('Office ZIP 文件目录或数据位置无效。');
    if(data.readUInt16LE(position+8)&1)throw new Error('不支持加密的 Office 文档包。');
    if(local+30+data.readUInt16LE(local+26)+data.readUInt16LE(local+28)+compressed>offset)throw new Error('Office ZIP 文件数据超出目录边界。');
    let extra=position+46+nameLength, extraEnd=extra+extraLength;
    while(extra<extraEnd) {
      if(extra+4>extraEnd)throw new Error('Office ZIP 扩展目录损坏。');
      if(data.readUInt16LE(extra)===1)throw new Error('Office 预览不支持 ZIP64 文档包。');
      extra+=4+data.readUInt16LE(extra+2);
    }
    if(extra!==extraEnd)throw new Error('Office ZIP 扩展目录损坏。');
    position=next;
  }
  if(position!==end)throw new Error('Office ZIP 中央目录长度不匹配。');
}
