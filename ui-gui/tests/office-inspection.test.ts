import {test} from 'node:test';
import assert from 'node:assert/strict';
import {zipSync, strToU8} from 'fflate';
import * as office from '../src/main/office-inspection.js';
const zip = (entries: Record<string,string>) => zipSync(Object.fromEntries(Object.entries(entries).map(([key,value])=>[key,strToU8(value)])));
const types = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>';
const sheet = '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><f>1+2</f><v>3</v></c><c r="C1"><f>NOW()</f></c></row><row r="203"><c r="A203"><v>42</v></c></row></sheetData></worksheet>';
const workbook = () => ({'[Content_Types].xml':types, '_rels/.rels':'<Relationships><Relationship Id="rId1" Target="xl/workbook.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/></Relationships>', 'xl/workbook.xml':'<workbook><sheets><sheet name="数据" sheetId="1" r:id="rId1"/></sheets></workbook>', 'xl/_rels/workbook.xml.rels':'<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/></Relationships>', 'xl/worksheets/sheet1.xml':sheet, 'xl/sharedStrings.xml':'<sst><si><r><t>中文</t></r><r><t>标题</t></r></si></sst>'});

test('XLSX renders cached values and flags formulas without recalculating',()=>{
 const result = office.spreadsheetPreview(zip(workbook()));
 assert.equal(result.checks.status,'passed'); assert.equal(result.sheets[0].name,'数据');
 assert.deepEqual(result.sheets[0].rows[0],['中文标题','3','（公式未缓存）']);
 assert.equal(result.sheets[0].rows.length,1); assert.equal(result.sheets[0].truncated,true);
 assert.match(result.warnings.join(' '),/未重新计算/); assert.match(result.warnings.join(' '),/未缓存/);
});

test('structural check rejects dangling relationships and malformed XML',()=>{
 const dangling = workbook(); dangling['xl/_rels/workbook.xml.rels']=dangling['xl/_rels/workbook.xml.rels'].replace('sheet1.xml','missing.xml');
 assert.throws(()=>office.inspectOffice(zip(dangling),'xlsx'),/引用缺失/);
 const malformed=workbook(); malformed['xl/workbook.xml']='<workbook><sheets></workbook>';
 assert.throws(()=>office.inspectOffice(zip(malformed),'xlsx'),/XML/);
});

test('structural check rejects DTDs and excessive ZIP expansion before rendering',()=>{
 const entity=workbook(); entity['xl/workbook.xml']='<!DOCTYPE doc [<!ENTITY x "expand">]><workbook/>';
 assert.throws(()=>office.inspectOffice(zip(entity),'xlsx'),/DTD|实体/);
 const large=workbook(); large['bomb.xml']='<a>'+'a'.repeat(17*1024*1024)+'</a>';
 assert.throws(()=>office.inspectOffice(zip(large),'xlsx'),/解压|大小/);
});

test('read-only Office check requires the requested main part and package relationships',()=>{
 assert.throws(()=>office.inspectOffice(zip(workbook()),'docx'),/word\/document.xml/);
 const entries=workbook(); delete (entries as Record<string,string>)['_rels/.rels'];
 assert.throws(()=>office.inspectOffice(zip(entries),'xlsx'),/关系|_rels/);
});

test('XLSX decodes numeric and predefined XML characters and labels empty numeric formula caches',()=>{
 const entries=workbook();entries['xl/sharedStrings.xml']='<sst><si><t>&#20013;&#25991; &amp; &lt;tag&gt;</t></si></sst>';
 entries['xl/worksheets/sheet1.xml']='<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><f>1+2</f><v></v></c></row></sheetData></worksheet>';
 assert.deepEqual(office.spreadsheetPreview(zip(entries)).sheets[0].rows[0],['中文 & <tag>','（公式未缓存）']);
});

test('Office preflight rejects ZIP64 and truncated central directories before archive decoding',()=>{
 const regular=Buffer.from(zip(workbook()));
 const eocd=regular.length-22;
 const zip64=Buffer.from(regular);zip64.writeUInt32LE(0xffffffff,eocd+16);
 assert.throws(()=>office.validateOfficeArchive(zip64),/ZIP64/);
 const malformed=Buffer.from(regular);malformed.writeUInt32LE(regular.length+10,eocd+16);
 assert.throws(()=>office.validateOfficeArchive(malformed),/目录/);
});

test('empty shared-string cells stay empty instead of displaying the first shared string',()=>{
 const entries=workbook();entries['xl/worksheets/sheet1.xml']='<worksheet><sheetData><row r="1"><c r="A1" t="s"/><c r="B1" t="s"><v/></c><c r="C1" t="inlineStr"><is><t>visible</t></is></c></row></sheetData></worksheet>';
 assert.deepEqual(office.spreadsheetPreview(zip(entries)).sheets[0].rows[0],['','','visible']);
});
