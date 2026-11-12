import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
const {chromium}=await import(process.env.AIST_PLAYWRIGHT);
const [phase,origin,statePath,out,pdfPath]=process.argv.slice(2);
assert(['prepare','live','inspect','reopen'].includes(phase));
const state=JSON.parse(await readFile(statePath,'utf8'));await mkdir(out,{recursive:true});
const headers={Authorization:'Bearer '+state.session.token},prefix=`/v1/projects/${state.project}`;
const api=async path=>{const r=await fetch(origin+'/api'+path,{headers});assert.equal(r.status,200);return r.json();};
const browser=await chromium.launch({channel:'chrome',headless:true});
const context=await browser.newContext({viewport:{width:1500,height:1080},acceptDownloads:true});
const page=await context.newPage();page.setDefaultTimeout(30000);
const errors=[],traffic=[],requests=[],checks=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('response',r=>{if(r.url().includes('/api/'))traffic.push({path:new URL(r.url()).pathname,status:r.status(),method:r.request().method()});});
page.on('request',r=>{if(r.method()==='POST'&&r.headers()['content-type']?.includes('application/json'))requests.push({path:new URL(r.url()).pathname.replace('/api',''),key:r.headers()['idempotency-key'],body:r.postDataJSON()});});
async function record(name){checks.push(name);await writeFile(out+'/progress.json',JSON.stringify({checks,errors,requests},null,2));console.log(name);}
async function login(result=''){
 await page.goto(origin);await page.evaluate(({s,result})=>{const put=(k,v)=>sessionStorage.setItem('aist.d08.'+k,JSON.stringify(v));put('session',s.session.token);put('project',s.project);put('selection.'+s.project,{conversation:s.conversation??'',selected:[s.source,...(s.source2?[s.source2]:[])],known:[s.source,...(s.source2?[s.source2]:[])],run:s.run??'',result});}, {s:state,result});await page.reload();
}
async function saveState(){await writeFile(statePath,JSON.stringify(state,null,2));}
async function result(){
 const id=state.run;let last;
 for(const until=Date.now()+900000;Date.now()<until;){const r=await api(prefix+'/runs/'+id);if(r.state!==last){console.log(id+' '+r.state);last=r.state;}
  if(r.state==='completed'){state.version=r.result_version_id;await saveState();return r;}
  if(['failed','cancelled','budget_exceeded','recovery_required','waiting_for_user'].includes(r.state))throw new Error(JSON.stringify(r));
  await new Promise(resolve=>setTimeout(resolve,1500));
 }throw new Error('Run timeout');
}
async function openResult(id){await login(id);await page.locator(`article[data-version-id="${id}"]`).waitFor();await page.locator('.basis-fact').first().waitFor();}
async function download(format,name){const wait=page.waitForEvent('download');await page.getByRole('button',{name:'Скачать '+format,exact:true}).first().click();await (await wait).saveAs(out+'/'+name);return createHash('sha256').update(await readFile(out+'/'+name)).digest('hex');}
async function checkSources(){
 const version=await api(prefix+'/artifact-versions/'+state.version);
 for(const vid of [state.source,state.source2]){
  const fact=version.version.basis.facts.find(f=>f.source.source_version_id===vid&&f.amount?.unit==='money');assert(fact);
  const source=await api(prefix+'/source-versions/'+vid);const number=fact.evidence[0].locator.pdf_page;
  const section=page.locator(`[id="basis-${vid}:${fact.fact_id}"]`);
  await section.getByRole('button',{name:`Основание: ${source.original_filename}, страница ${number}`,exact:true}).first().click();
  await page.getByRole('dialog').waitFor();assert.equal(await page.getByLabel('Страница',{exact:true}).inputValue(),String(number));
  assert.equal(await page.locator('#pdf-title').innerText(),source.original_filename);
  await page.locator('.page-text pre').filter({hasNotText:'Загрузка текста…'}).waitFor();
  const text=await api(prefix+'/source-versions/'+vid+'/page?page='+number);
  assert.equal(await page.locator('.page-text pre').innerText(),text.text);
  await page.screenshot({path:out+`/source-${vid}.png`});await page.getByRole('button',{name:'Закрыть PDF',exact:true}).click();
 }
 const badLinks=await page.locator('.claim-links a').evaluateAll(links=>links.filter(a=>!document.getElementById(decodeURIComponent(a.hash.slice(1)))).map(a=>a.href));assert.deepEqual(badLinks,[]);
 await record('both_original_pdfs_open_at_cited_pages_and_qualified_links_resolve');
 return version;
}
try{
 if(phase==='prepare'){
  await login();await page.locator('.source-item').first().waitFor();if(!await page.getByLabel('Название разговора').isVisible())await page.getByText('Новый разговор',{exact:true}).click();await page.getByLabel('Название разговора').fill('D14: два документа Whoosh');
  const convResponse=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/conversations'));
  await page.getByRole('button',{name:'Создать разговор',exact:true}).click();const conv=await (await convResponse).json();state.conversation=conv.id;await saveState();
  if(!await page.getByLabel('Файл PDF',{exact:true}).isVisible())await page.getByText('Загрузить PDF или новую версию',{exact:true}).click();await page.getByLabel('Название источника').fill('Whoosh: отчетность МСФО за девять месяцев');await page.getByLabel('Файл PDF',{exact:true}).setInputFiles(pdfPath);
  const uploadResponse=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/sources'));
  await page.getByRole('button',{name:'Загрузить PDF',exact:true}).click();const response=await uploadResponse;assert.equal(response.status(),201);const upload=await response.json();state.source2=upload.version.id;state.source2_parent=upload.source.id;await saveState();
  assert.equal(upload.version.page_count,25);assert.equal(upload.version.byte_size,777294);
  await page.locator('.source-item').filter({has:page.getByRole('heading',{name:'Whoosh: отчетность МСФО за девять месяцев',exact:true})}).getByRole('checkbox').check();
  await page.locator('.source-list input:checked').nth(1).waitFor();assert.equal(await page.locator('.source-list input:checked').count(),2);
  await page.getByLabel('Навык',{exact:true}).selectOption('skill_analytical-note-v2');assert(await page.getByRole('button',{name:'Отправить задачу',exact:true}).isDisabled());
  await page.getByLabel('Навык',{exact:true}).selectOption('skill_analytical-note-v3');await page.getByLabel('Аудитория',{exact:true}).fill('Руководитель');
  await page.screenshot({path:out+'/selected-sources.png',fullPage:true});await record('new_pdf_uploaded_through_ui_and_two_versions_selected');
  await writeFile(out+'/result.json',JSON.stringify({passed:true,checks,errors,upload,conversation:conv,traffic},null,2));
 }else if(phase==='live'){
  await login();await page.getByLabel('Навык',{exact:true}).selectOption('skill_analytical-note-v3');await page.getByLabel('Аудитория',{exact:true}).fill('Руководитель');
  await page.getByLabel('Задача по источникам').fill('Подготовь краткую записку для руководителя по двум материалам Whoosh. Сопоставь выручку за девять месяцев двух лет, объясни различия представления чисел, рассчитай рост по сопоставимым данным. Отдельно сохрани определение и ограничения EBITDA. У каждого вывода укажи документ и страницу');
  await page.locator('.source-list input:checked').nth(1).waitFor();assert.equal(await page.locator('.source-list input:checked').count(),2);
  await page.locator('form.composer').screenshot({path:out+'/request.png'});
  const sent=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/messages'));
  await page.getByRole('button',{name:'Отправить задачу',exact:true}).click();const response=await sent;assert.equal(response.status(),202);const submission=await response.json();state.run=submission.run.id;await saveState();
  await writeFile(out+'/submission.json',JSON.stringify({submission,requests},null,2));await record('exact_user_request_submitted_through_ui');
  const run=await result();await openResult(state.version);const version=await checkSources();assert.equal(version.review.acceptance,null);
  assert.equal(new Set(version.version.basis.facts.map(f=>f.source.source_version_id)).size,2);
  await page.screenshot({path:out+'/generated-note.png',fullPage:true});await record('live_note_saved_with_both_sources');
  await writeFile(out+'/result.json',JSON.stringify({passed:true,checks,errors,requests,run,version,traffic},null,2));
 }else if(phase==='inspect'){
  await openResult(state.version);const original=await checkSources();
  await page.getByRole('button',{name:'Редактировать записку',exact:true}).click();await page.getByRole('button',{name:'Добавить ручной абзац',exact:true}).last().click();
  await page.getByLabel('Абзац',{exact:true}).last().fill('Редакторская оговорка: перед внешним использованием необходимо сверить актуальность материалов и согласовать выводы.');
  const saved=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/versions'));
  await page.getByRole('button',{name:'Сохранить новую версию',exact:true}).click();const response=await saved;assert.equal(response.status(),201);const edited=await response.json();state.edited=edited.version.id;await saveState();assert.equal(edited.review.acceptance,null);assert.deepEqual(edited.version.basis,original.version.basis);
  await page.locator(`article[data-version-id="${state.edited}"]`).waitFor();await record('manual_edit_new_version_keeps_basis_and_no_inherited_acceptance');
  await page.getByLabel('Статус выпуска').selectOption('draft');await page.getByRole('button',{name:'Выпустить DOCX и PDF',exact:true}).click();await page.locator('.export-list li[data-state="ready"]').nth(1).waitFor({timeout:120000});
  const downloads={docx:await download('DOCX','draft.docx'),pdf:await download('PDF','draft.pdf')};state.downloads=downloads;await saveState();
  await page.getByRole('button',{name:'Просмотреть PDF',exact:true}).click();await page.locator('dialog[aria-label="Готовый PDF"] object').waitFor();await page.screenshot({path:out+'/pdf-preview.png'});await page.getByRole('button',{name:'Закрыть PDF выпуска',exact:true}).click();
  await page.reload();await page.getByRole('button',{name:'Скачать PDF',exact:true}).waitFor();assert.equal(await download('DOCX','reload.docx'),downloads.docx);assert.equal(await download('PDF','reload.pdf'),downloads.pdf);
  await page.screenshot({path:out+'/edited-note.png',fullPage:true});await record('draft_exports_saved_and_reload_downloads_same_sha');
  assert.deepEqual(errors,[]);await writeFile(out+'/result.json',JSON.stringify({passed:true,checks,errors,requests,edited,downloads,traffic},null,2));
 }else{
  await openResult(state.edited);await page.getByRole('button',{name:'Скачать PDF',exact:true}).waitFor();
  assert.equal(await download('DOCX','restart.docx'),state.downloads.docx);assert.equal(await download('PDF','restart.pdf'),state.downloads.pdf);await checkSources();
  await page.screenshot({path:out+'/restart.png',fullPage:true});await record('restart_keeps_note_and_downloads');assert.equal(requests.length,0);
  await writeFile(out+'/result.json',JSON.stringify({passed:true,checks,errors,requests,traffic},null,2));
 }
 assert.deepEqual(errors,[]);
}catch(e){await page.screenshot({path:out+'/failure.png',fullPage:true}).catch(()=>{});await writeFile(out+'/failure.txt',String(e)+'\n'+await page.locator('body').innerText());throw e;}finally{await browser.close();}
