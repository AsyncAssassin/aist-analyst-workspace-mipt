import assert from 'node:assert/strict';
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
const root=resolve(import.meta.dirname,'../..');
const {chromium}=await import(pathToFileURL(resolve(root,'.runtime/qa-tools/node_modules/playwright/index.mjs')));
const out=resolve(process.env.D08_EVIDENCE ?? root+'/.runtime/visual');await mkdir(out,{recursive:true});
const session=JSON.parse(await readFile(root+'/.runtime/private/sessions.json','utf8')).analyst;
const browser=await chromium.launch({channel:'chrome',headless:process.env.D08_HEADED !== 'true'});
const context=await browser.newContext({viewport:{width:1280,height:720},locale:'ru-RU',timezoneId:'Asia/Tbilisi'});
const page=await context.newPage();const checks=[];
try{
 await page.goto('http://127.0.0.1:28911');await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.className),'skip-link');await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.type),'password');await page.keyboard.type(session.token);await page.keyboard.press('Tab');await page.keyboard.press('Enter');await page.locator('.project-list button').waitFor();
 await page.locator('.project-list button').first().focus();await page.keyboard.press('Enter');await page.locator('.project-heading h1').waitFor();await page.getByRole('combobox',{name:'Разговор',exact:true}).selectOption({label:'Проверка источников'});await page.getByRole('button',{name:'Сохранённый результат',exact:true}).click();await page.getByText('Синтетический результат QA',{exact:true}).waitFor();await page.locator('.result-card .actions button').waitFor();await page.evaluate(()=>document.fonts.ready);
 for(const [width,height,name] of [[1280,720,'result-laptop'],[820,900,'result-narrow'],[390,844,'result-mobile']]){
  await page.setViewportSize({width,height});await page.evaluate(()=>scrollTo(0,0));assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await page.screenshot({path:out+'/'+name+'.png',fullPage:false});checks.push({width,height,horizontal_overflow:false});
 }
 await page.setViewportSize({width:1280,height:800});await page.getByRole('button',{name:'alpha.pdf · версия 1',exact:true}).click();await page.getByRole('combobox',{name:'Страница',exact:true}).selectOption('2');await page.getByText('Synthetic AIST alpha. Page 2.',{exact:false}).waitFor();await page.waitForTimeout(3000);await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:out+'/pdf-page-2.png',fullPage:false});
 const blob=await page.locator('object').getAttribute('data');const bytes=await page.evaluate(async url=>Array.from(new Uint8Array(await (await fetch(url.split('#')[0])).arrayBuffer())),blob);const hash=createHash('sha256').update(Buffer.from(bytes)).digest('hex');const original=createHash('sha256').update(await readFile(root+'/.runtime/fixtures/alpha.pdf')).digest('hex');assert.equal(hash,original);
 const downloadEvent=page.waitForEvent('download');await page.getByRole('link',{name:'Скачать оригинал',exact:true}).click();const download=await downloadEvent;assert.equal(download.suggestedFilename(),'alpha.pdf');
 await page.keyboard.press('Escape');assert.equal(await page.locator('dialog').count(),0);await assert.rejects(page.evaluate(url=>fetch(url),blob.split('#')[0]));
 const active=await page.evaluate(()=>({tag:document.activeElement.tagName,text:document.activeElement.textContent}));
 await writeFile(out+'/visual.json',JSON.stringify({passed:true,keyboard_login:true,keyboard_project_selection:true,escape_closes_pdf:true,blob_revoked:true,pdf_bytes_sha256:hash,original_bytes_match:true,download_filename:download.suggestedFilename(),focus_after_pdf:active,viewports:checks},null,2));
 console.log('PASS keyboard, three viewports, PDF page 2, original bytes/download, Blob revocation');
}finally{await browser.close();}
