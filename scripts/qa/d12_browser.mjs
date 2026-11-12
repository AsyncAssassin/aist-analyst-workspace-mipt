import {chromium} from 'playwright';
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {compose, webOrigin} from './local_compose.mjs';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
const state=JSON.parse(await readFile('.runtime/private/state.json','utf8'));
const out=process.cwd()+'/.runtime/evidence/browser';await mkdir(out,{recursive:true});
const base=webOrigin,prefix=`/v1/projects/${state.project}`,headers={Authorization:'Bearer '+state.session.token};
const api=async p=>{const r=await fetch(base+'/api'+p,{headers});assert.equal(r.status,200);return r.json();};
const browser=await chromium.launch({channel:'chrome',headless:true});const context=await browser.newContext({viewport:{width:1440,height:1050},acceptDownloads:true}),page=await context.newPage();
page.setDefaultTimeout(20000);const errors=[],traffic=[],checks=[];page.on('pageerror',e=>errors.push(e.message));page.on('response',r=>{if(r.url().includes('/api/'))traffic.push({path:new URL(r.url()).pathname,status:r.status(),method:r.request().method()});});
async function check(name){checks.push(name);console.log(name);await writeFile(out+'/progress.json',JSON.stringify({checks,traffic,errors},null,2));}
async function selected(id){await page.getByLabel('Сохраненные версии').selectOption(id);await page.locator(`article[data-version-id="${id}"]`).waitFor();const v=await api(prefix+'/artifact-versions/'+id);await page.getByRole('heading',{name:'DOCX и PDF выбранной v'+v.version.revision,exact:true}).waitFor();}
async function save(number){await page.getByRole('button',{name:'Сохранить новую версию'}).click();await page.locator('.result-card .small.muted').filter({hasText:`Версия ${number} ·`}).first().waitFor();return page.locator('article.result-card').getAttribute('data-version-id');}
async function release(mode){await page.getByLabel('Статус выпуска').selectOption(mode);await page.getByRole('button',{name:'Выпустить DOCX и PDF',exact:true}).click();await page.locator('.export-list li[data-state="ready"]').nth(1).waitFor({timeout:120000});}
async function download(format,filename){const item=page.locator('.export-list li').filter({hasText:filename.includes('v2')?'Принятая версия':'Черновик'}).filter({has:page.getByRole('button',{name:'Скачать '+format,exact:true})}).first();const wait=page.waitForEvent('download');await item.getByRole('button',{name:'Скачать '+format,exact:true}).click();const d=await wait;await d.saveAs(out+'/'+filename);const data=await readFile(out+'/'+filename);return createHash('sha256').update(data).digest('hex');}
try{
 await page.goto(base);await page.evaluate(s=>{const put=(k,v)=>sessionStorage.setItem('aist.d08.'+k,JSON.stringify(v));put('session',s.session.token);put('project',s.project);put('selection.'+s.project,{conversation:s.conversation,selected:[s.source],known:[s.source],run:s.run,result:s.version});},state);await page.reload();await page.getByRole('button',{name:'Редактировать записку'}).waitFor();
 const v1=await api(prefix+'/artifact-versions/'+state.version);
 const existing=(await api(prefix+'/artifacts/'+state.artifact+'/versions')).items;let v2id,v2,v3id;
 if(existing.some(v=>v.revision===3)){v2id=existing.find(v=>v.revision===2).id;v3id=existing.find(v=>v.revision===3).id;v2=await api(prefix+'/artifact-versions/'+v2id);checks.push(...JSON.parse(await readFile(out+'/progress.json')).checks.slice(0,3));}else{
 await page.getByRole('button',{name:'Редактировать записку'}).click();
 await page.getByLabel('Подпись таблицы',{exact:true}).fill('Выручка ВУШ Холдинг, млн RUB; рост выручки, %; периоды по источнику');
 await page.getByRole('button',{name:'Добавить ручной абзац',exact:true}).first().click();
 await page.getByLabel('Абзац',{exact:true}).filter({hasText:''}).count();
 const empty=page.locator('textarea').filter({hasText:''});
 const texts=page.getByLabel('Абзац',{exact:true});for(let i=0;i<await texts.count();i++){if(await texts.nth(i).inputValue()===''){await texts.nth(i).fill('Редакторская оговорка: перед использованием записки необходимо сверить актуальность исходных материалов.');break;}}
 assert(await page.getByRole('button',{name:'Выпустить DOCX и PDF',exact:true}).isDisabled());await check('unsaved_form_blocks_export');
 v2id=await save(2);await page.getByRole('checkbox',{name:'Принимаю выбранную сохраненную версию без смысловой проверки'}).check();await page.getByRole('button',{name:'Принять выбранную версию',exact:true}).click();await page.getByText('Принята человеком:',{exact:false}).waitFor();
 v2=await api(prefix+'/artifact-versions/'+v2id);assert(v2.review.acceptance);assert.equal(v2.review.acceptance.content_sha256,v2.version.content_sha256);assert.equal(v2.checks.find(x=>x.kind==='semantic').status,'not_checked');await check('manual_v2_separately_accepted_exact_sha');
 await page.getByRole('button',{name:'Редактировать записку'}).click();await page.getByLabel('Аудитория',{exact:true}).fill('Команда аналитиков и редакторов');v3id=await save(3);assert.equal((await api(prefix+'/artifact-versions/'+v3id)).review.acceptance,null);await check('v3_does_not_inherit_acceptance');}
 await selected(v2id);await release('accepted');await check('historical_v2_both_exports_ready');
 const docxSha=await download('DOCX','accepted-v2.docx'),pdfSha=await download('PDF','accepted-v2.pdf');
 const exports=(await api(prefix+`/artifacts/${state.artifact}/versions/${v2id}/exports`)).items.filter(e=>e.settings.release_mode==='accepted');assert.equal(exports.length,2);for(const e of exports){assert.equal(e.artifact_version_id,v2id);assert.equal(e.content_sha256,v2.version.content_sha256);assert.equal(e.acceptance_id,v2.review.acceptance.id);}
 await page.locator('.export-list li').filter({hasText:'Принятая версия'}).getByRole('button',{name:'Просмотреть PDF',exact:true}).click();await page.locator('dialog[aria-label="Готовый PDF"] object').waitFor();await page.waitForTimeout(1800);await page.screenshot({path:out+'/01-pdf-view.png'});await page.getByRole('button',{name:'Закрыть PDF выпуска'}).click();assert.equal(await page.locator('article.result-card').getAttribute('data-version-id'),v2id);await check('pdf_preview_keeps_selected_version');
 await page.reload();await page.getByRole('button',{name:'Скачать DOCX'}).first().waitFor();assert.equal(await download('DOCX','reload-v2.docx'),docxSha);assert.equal(await download('PDF','reload-v2.pdf'),pdfSha);await check('reload_same_download_sha');
 compose('restart','backend','worker','web');await page.waitForTimeout(5000);await page.reload();await page.getByRole('button',{name:'Скачать PDF'}).first().waitFor();assert.equal(await download('DOCX','restart-v2.docx'),docxSha);assert.equal(await download('PDF','restart-v2.pdf'),pdfSha);await check('restart_same_download_sha');
 await page.getByRole('button',{name:'Выпустить DOCX и PDF',exact:true}).click();await page.getByRole('button',{name:'Выпустить DOCX и PDF',exact:true}).waitFor();
 // Reload defaults to draft: this is a deliberate distinct release, not an identity retry.
 await page.locator('.export-list li[data-state="ready"]').nth(3).waitFor({timeout:120000});await check('accepted_version_can_have_explicit_draft');
 await selected(v3id);await page.waitForFunction(()=>document.querySelector('select[aria-label="Статус выпуска"] option[value="accepted"]')?.disabled===true);await release('draft');await download('DOCX','draft-v3.docx');await download('PDF','draft-v3.pdf');await check('unaccepted_v3_draft_export');
 await page.getByLabel('Оформление выпуска').selectOption('compact');await page.getByRole('button',{name:'Выпустить DOCX и PDF',exact:true}).click();await page.locator('.export-list li[data-state="ready"]').nth(3).waitFor({timeout:120000});await check('compact_separate_identity');
 await selected(v2id);await page.screenshot({path:out+'/02-exports-history.png'});
 await page.getByRole('button',{name:'Основание: страница 11, 9M’21',exact:true}).first().click();await page.locator('.pdf-original object').waitFor();await page.getByRole('button',{name:'Закрыть PDF',exact:true}).click();await check('source_basis_pdf_regression');
 assert.deepEqual((await api(prefix+'/artifact-versions/'+state.version)).version,v1.version);assert.deepEqual(errors,[]);
 await writeFile(out+'/v2.json',JSON.stringify(v2,null,2));await writeFile(out+'/result.json',JSON.stringify({passed:true,checks,traffic,errors,v1:state.version,v2:v2id,v3:v3id,exports,docxSha,pdfSha},null,2));
}catch(e){await page.screenshot({path:out+'/failure.png',fullPage:true}).catch(()=>{});await writeFile(out+'/failure.txt',String(e)+'\n'+await page.locator('body').innerText());throw e;}finally{await browser.close();}
