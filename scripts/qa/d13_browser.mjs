import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
const {chromium}=await import(process.env.AIST_PLAYWRIGHT);
const [phase,origin,statePath,out]=process.argv.slice(2);
assert(['prepare','live','reopen','answer-retry'].includes(phase));
const state=JSON.parse(await readFile(statePath,'utf8'));await mkdir(out,{recursive:true});
const prefix=`/v1/projects/${state.project}`,headers={Authorization:'Bearer '+state.session.token};
const api=async path=>{const r=await fetch(origin+'/api'+path,{headers});assert.equal(r.status,200);return r.json();};
const history=(await api(prefix+'/artifacts/'+state.artifact+'/versions')).items;
const chosen=history.find(v=>v.revision===2);assert(chosen);
const accepted=await api(prefix+'/artifact-versions/'+chosen.id);assert(accepted.review.acceptance);
const browser=await chromium.launch({channel:'chrome',headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1050},acceptDownloads:true});
const page=await context.newPage();page.setDefaultTimeout(30000);
const errors=[],traffic=[],requests=[],checks=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('response',r=>{if(r.url().includes('/api/'))traffic.push({path:new URL(r.url()).pathname,status:r.status(),method:r.request().method()});});
page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/messages'))requests.push({path:new URL(r.url()).pathname.replace('/api',''),key:r.headers()['idempotency-key'],body:r.postDataJSON()});});
async function record(name){checks.push(name);await writeFile(out+'/progress.json',JSON.stringify({checks,errors,requests},null,2));console.log(name);}
async function login(result){await page.goto(origin);await page.evaluate(({s,result})=>{const put=(k,v)=>sessionStorage.setItem('aist.d08.'+k,JSON.stringify(v));put('session',s.session.token);put('project',s.project);put('selection.'+s.project,{conversation:s.conversation,selected:[s.source],known:[s.source],run:s.run,result});}, {s:state,result});await page.reload();}
async function waitRun(){
 await page.locator('.run-card').waitFor();const id=await page.locator('.run-card').getAttribute('data-run-id');
 let last;const until=Date.now()+900000;
 while(Date.now()<until){const r=await api(prefix+'/runs/'+id);if(r.state!==last){console.log(id+' '+r.state);last=r.state;}
  if(r.state==='completed'){await page.locator('.run-card').getByRole('button',{name:'Открыть результат',exact:true}).click();await page.locator(`article[data-version-id="${r.result_version_id}"]`).waitFor();return r;}
  if(['failed','cancelled','budget_exceeded','recovery_required','waiting_for_user'].includes(r.state))throw new Error(JSON.stringify(r));
  await new Promise(resolve=>setTimeout(resolve,1500));
 }
 throw new Error('Run timeout');
}
async function download(format,name){const wait=page.waitForEvent('download');await page.getByRole('button',{name:'Скачать '+format,exact:true}).first().click();const d=await wait;await d.saveAs(out+'/'+name);return createHash('sha256').update(await readFile(out+'/'+name)).digest('hex');}
try{
 if(phase==='answer-retry'){
  const note=history.find(v=>v.revision===11);assert(note);
  await login(note.id);await page.getByRole('button',{name:'К разговору',exact:true}).waitFor();await page.getByRole('button',{name:'К разговору',exact:true}).click();
  await page.getByLabel('Навык',{exact:true}).selectOption('skill_source-answer-v1');
  await page.getByLabel('Задача по источникам').fill('К чему относится рентабельность EBITDA 58% и что исключено из показателя?');
  await page.locator('form.composer').screenshot({path:out+'/01-question.png'});
  const submission=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/messages'));
  await page.getByRole('button',{name:'Отправить задачу',exact:true}).click();const response=await submission;assert.equal(response.status(),202);const submitted=await response.json();
  await page.locator(`.run-card[data-run-id="${submitted.run.id}"]`).waitFor();await record('short_question_sent_through_ui_after_schema_repair');
  const answer=await waitRun();const answerVersion=await api(prefix+'/artifact-versions/'+answer.result_version_id);assert.equal(answerVersion.version.payload.result_type,'evidence_answer');
  assert.equal(await page.getByRole('button',{name:'Выпустить DOCX и PDF',exact:true}).count(),0);assert.equal(requests[0].body.base_version_id,null);
  await page.screenshot({path:out+'/02-answer.png',fullPage:true});await page.reload();await page.locator(`article[data-version-id="${answer.result_version_id}"]`).waitFor();
  await record('short_answer_saved_reload_no_export');assert.deepEqual(errors,[]);await writeFile(out+'/result.json',JSON.stringify({passed:true,checks,errors,requests,answer,answerVersion,traffic},null,2));
 }else if(phase==='reopen'){
  const live=JSON.parse(await readFile(out+'/../live/result.json','utf8'));
  await login(live.note.result_version_id);await page.getByRole('button',{name:'Скачать PDF',exact:true}).waitFor();
  assert.equal(await download('DOCX','restart-draft.docx'),live.downloads.docx);assert.equal(await download('PDF','restart-draft.pdf'),live.downloads.pdf);
  await record('restart_saved_downloads_same_sha');
  await page.getByRole('button',{name:'К разговору',exact:true}).click();await page.getByRole('button',{name:'Сохранённый результат',exact:true}).last().click();
  await page.locator(`article[data-version-id="${live.answer.result_version_id}"]`).waitFor();assert.equal(await page.locator('.export-card').count(),0);await page.screenshot({path:out+'/answer-after-restart.png'});
  await record('restart_short_answer_available');
  assert.deepEqual(errors,[]);await writeFile(out+'/result.json',JSON.stringify({passed:true,checks,errors,traffic},null,2));
 }else{
  await login(state.version);await page.getByLabel('Сохраненные версии').waitFor();await page.getByLabel('Сохраненные версии').selectOption(chosen.id);
  await page.locator(`article[data-version-id="${chosen.id}"]`).waitFor();await page.getByText('Принята человеком:',{exact:false}).waitFor();await page.screenshot({path:out+'/01-selected-v2.png'});
  await page.getByRole('button',{name:'Продолжить от выбранной версии',exact:true}).click();
  assert.equal(await page.getByLabel('Навык',{exact:true}).inputValue(),'skill_analytical-note-v2');
  await page.getByLabel('Аудитория',{exact:true}).fill('Руководитель');await page.getByLabel('Разделы через запятую').fill('Главное, Показатели, Ограничения');
  await page.getByLabel('Задача по источникам').fill('Сократи записку для руководителя до разделов Главное, Показатели и Ограничения. Сохрани числовую таблицу, основания, оговорку EBITDA и мой редакторский абзац');
  await page.reload();await page.getByLabel('Аудитория',{exact:true}).waitFor();assert.equal(await page.getByLabel('Аудитория',{exact:true}).inputValue(),'Руководитель');
  await page.locator('form.composer').screenshot({path:out+'/02-continuation-request.png'});
  const submission=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/messages'));
  await page.getByRole('button',{name:'Отправить задачу',exact:true}).click();const r=await submission;assert.equal(r.status(),202);const noteSubmission=await r.json();
  assert.equal(requests[0].body.base_version_id,chosen.id);assert.equal(requests[0].body.expected_artifact_revision,Math.max(...history.map(v=>v.revision)));
  assert.deepEqual(requests[0].body.parameters,{audience:'Руководитель',sections:['Главное','Показатели','Ограничения']});
  await record('selected_v2_parameters_exact_ui_submission');
  if(phase==='prepare'){
   assert.equal(noteSubmission.run.state,'queued');await writeFile(out+'/result.json',JSON.stringify({passed:true,synthetic_preparation:true,chosen:chosen.id,checks,errors,requests,noteSubmission},null,2));
  }else{
   const note=await waitRun();const newVersion=await api(prefix+'/artifact-versions/'+note.result_version_id);
   assert.equal(newVersion.version.parent_version_id,chosen.id);assert.equal(newVersion.version.artifact_id,state.artifact);assert.equal(newVersion.review.acceptance,null);
   assert.deepEqual(newVersion.version.payload.content.sections.map(s=>s.title),['Главное','Показатели','Ограничения']);await page.screenshot({path:out+'/03-new-note.png',fullPage:true});await record('live_note_saved_from_selected_v2');
   await page.getByLabel('Статус выпуска').selectOption('draft');await page.getByRole('button',{name:'Выпустить DOCX и PDF',exact:true}).click();await page.locator('.export-list li[data-state="ready"]').nth(1).waitFor({timeout:120000});
   const downloads={docx:await download('DOCX','draft.docx'),pdf:await download('PDF','draft.pdf')};
   await page.getByRole('button',{name:'Просмотреть PDF',exact:true}).click();await page.locator('dialog[aria-label="Готовый PDF"] object').waitFor();await page.waitForTimeout(1500);await page.screenshot({path:out+'/04-pdf-view.png'});await page.getByRole('button',{name:'Закрыть PDF выпуска'}).click();
   await page.reload();await page.getByRole('button',{name:'Скачать PDF',exact:true}).waitFor();assert.equal(await download('DOCX','reload-draft.docx'),downloads.docx);assert.equal(await download('PDF','reload-draft.pdf'),downloads.pdf);await record('draft_exports_preview_reload_same_sha');
   await page.getByRole('button',{name:'К разговору',exact:true}).click();await page.getByLabel('Навык',{exact:true}).selectOption('skill_source-answer-v1');
   await page.getByLabel('Задача по источникам').fill('К чему относится рентабельность EBITDA 58% и что исключено из показателя?');
   await page.locator('form.composer').screenshot({path:out+'/05-short-question.png'});await page.getByRole('button',{name:'Отправить задачу',exact:true}).click();
   await page.waitForFunction(old=>document.querySelector('.run-card')?.getAttribute('data-run-id')!==old,note.id);
   const answer=await waitRun();const answerVersion=await api(prefix+'/artifact-versions/'+answer.result_version_id);assert.equal(answerVersion.version.payload.result_type,'evidence_answer');
   assert.equal(await page.getByRole('button',{name:'Выпустить DOCX и PDF',exact:true}).count(),0);assert.equal(requests[1].body.base_version_id,null);await page.screenshot({path:out+'/06-short-answer.png',fullPage:true});
   await page.reload();await page.locator(`article[data-version-id="${answer.result_version_id}"]`).waitFor();await record('short_answer_saved_reload_no_export');
   assert.deepEqual(errors,[]);await writeFile(out+'/result.json',JSON.stringify({passed:true,checks,errors,requests,note,answer,newVersion,answerVersion,downloads,traffic},null,2));
  }
 }
}catch(e){await page.screenshot({path:out+'/failure.png',fullPage:true}).catch(()=>{});await writeFile(out+'/failure.txt',String(e)+'\n'+await page.locator('body').innerText());throw e;}finally{await browser.close();}
