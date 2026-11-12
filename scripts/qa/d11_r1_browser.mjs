import {chromium} from 'playwright';
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {compose, webOrigin} from './local_compose.mjs';
import assert from 'node:assert/strict';
const s=JSON.parse(await readFile('.runtime/private/state.json','utf8')),base=webOrigin,out='.runtime/evidence/r1-browser';await mkdir(out,{recursive:true});
const cli=(...args)=>JSON.parse(compose('exec','-T','backend','python','-m','app.cli',...args));
const api=async path=>{const r=await fetch(base+'/api/v1/projects/'+s.project+path,{headers:{Authorization:'Bearer '+s.session.token}});assert.equal(r.status,200);return r.json();};
const b=await chromium.launch({channel:'chrome',headless:true}),p=await b.newPage({viewport:{width:1440,height:1050}});p.setDefaultTimeout(20000);const checks=[],errors=[],sessions=[];p.on('pageerror',e=>errors.push(e.message));
const savePath=`**/api/v1/projects/${s.project}/artifacts/${s.artifact}/versions`,draftKey=`aist.d08.editor.${s.project}.${s.version}`,pendingKey=draftKey+'.save';
const storage=key=>p.evaluate(k=>JSON.parse(sessionStorage.getItem(k)),key);
async function check(name){checks.push(name);console.log(name);}
async function seed(session){await p.goto(base);await p.evaluate(({s,session})=>{sessionStorage.clear();for(const[k,v]of Object.entries({session:session.token,project:s.project,['selection.'+s.project]:{conversation:s.conversation,selected:[s.source],known:[s.source],run:s.run,result:s.version}}))sessionStorage.setItem('aist.d08.'+k,JSON.stringify(v));},{s,session});await p.reload();await p.getByRole('button',{name:'Редактировать записку'}).click();}
async function login(session){await p.getByLabel('Токен сессии').fill(session.token);await p.getByRole('button',{name:'Войти',exact:true}).click();}
try{
 await seed(s.session);const before=await api('/artifacts/'+s.artifact);
 const {version:{basis}}=await api('/artifact-versions/'+s.version);
 const previous=basis.calculations.find(c=>c.formula_id==='growth_percent.v1').inputs.find(i=>i.name==='previous').fact;
 const revenue=basis.facts.find(f=>f.fact_id===previous.fact_id && f.source.source_version_id===previous.source_version_id).amount.value;
 for(const text of [`Выручка ${revenue} млн $.`,`Выручка ${revenue} млн RUB за 1M’21.`]){
  await p.getByLabel('Абзац',{exact:true}).nth(1).fill(text);await p.getByRole('button',{name:'Сохранить новую версию',exact:true}).click();await p.getByRole('alert').filter({hasText:'не прошло проверку оснований'}).waitFor();
  assert.equal(await p.getByLabel('Абзац',{exact:true}).nth(1).inputValue(),text);assert.equal((await api('/artifacts/'+s.artifact)).revision,before.revision);
 }
 await p.getByRole('alert').scrollIntoViewIfNeeded();await p.screenshot({path:out+'/01-invalid-period-keeps-draft.png'});await check('F01_F02_browser_reject_preserves_text_and_head');
 p.once('dialog',d=>d.accept());await p.getByRole('button',{name:'Отменить правки'}).click();
 const first=cli('issue-session','--user-id',s.actor);sessions.push(first);await seed(first);
 const text='Правка переживает потерю ответа и отзыв сессии';await p.getByLabel('Аудитория',{exact:true}).fill(text);
 let committed;
 await p.route(savePath,async route=>{if(route.request().method()==='POST'){const response=await route.fetch();assert.equal(response.status(),201);committed=await response.json();await route.abort('connectionreset');}else await route.continue();});
 await p.getByRole('button',{name:'Сохранить новую версию',exact:true}).click();await p.getByRole('alert').filter({hasText:'Нет ответа от сервиса'}).waitFor();await p.unroute(savePath);
 const envelope=await storage(pendingKey);assert(envelope);assert(committed);assert.equal(cli('revoke-session','--session-id',first.session_id).revoked,true);
 await p.getByRole('button',{name:'Проверить сохранение'}).click();await p.getByRole('alert').filter({hasText:'Сессия'}).waitFor();
 assert.equal(await p.getByLabel('Аудитория',{exact:true}).inputValue(),text);assert.deepEqual(await storage(pendingKey),envelope);assert.equal(await storage('aist.d08.session'),null);await check('real_401_keeps_draft_and_original_retry_envelope');
 p.once('dialog',d=>d.accept());await p.reload();await p.getByLabel('Токен сессии').waitFor();assert.equal((await storage(draftKey)).payload.content.audience,text);assert.deepEqual(await storage(pendingKey),envelope);await check('reload_after_revocation_keeps_draft');
 const renewed=cli('issue-session','--user-id',s.actor);sessions.push(renewed);await login(renewed);await p.getByRole('button',{name:'Редактировать записку'}).click();assert.equal(await p.getByLabel('Аудитория',{exact:true}).inputValue(),text);
 let sent;await p.route(savePath,async route=>{if(route.request().method()==='POST')sent={key:route.request().headers()['idempotency-key'],body:route.request().postData()};await route.continue();});
 await p.getByRole('button',{name:'Проверить сохранение'}).click();await p.getByRole('button',{name:'Редактировать записку'}).waitFor();await p.unroute(savePath);
 assert.equal(await p.locator('.result-card').getAttribute('data-version-id'),committed.version.id);assert.deepEqual(sent,{key:envelope.key,body:envelope.body});assert.equal((await api('/artifacts/'+s.artifact)).revision,committed.version.revision);await check('same_owner_login_replays_exact_commit_without_duplicate');await p.screenshot({path:out+'/02-restored-saved-version.png'});
 await p.getByRole('button',{name:'Редактировать записку'}).click();await p.getByLabel('Аудитория',{exact:true}).fill('Эту правку удалить явно');p.once('dialog',d=>d.accept());await p.getByRole('button',{name:'Отменить правки'}).click();await p.reload();await p.getByRole('button',{name:'Редактировать записку'}).click();assert.equal(await p.getByLabel('Аудитория',{exact:true}).inputValue(),text);await check('explicit_discard_survives_reload');
 await p.getByLabel('Аудитория',{exact:true}).fill('Частный черновик первого владельца');cli('revoke-session','--session-id',renewed.session_id);await p.getByRole('button',{name:'Сохранить новую версию',exact:true}).click();await p.getByRole('alert').filter({hasText:'Сессия'}).waitFor();
 p.once('dialog',d=>d.accept());await p.reload();const other=cli('create-user','--label','D11-R1 другая учетная запись');const otherSession=cli('issue-session','--user-id',other.user_id);sessions.push(otherSession);await login(otherSession);await p.getByRole('heading',{name:'Начните с проекта'}).waitFor();
 assert.equal(await p.getByText('Частный черновик первого владельца',{exact:true}).count(),0);assert.deepEqual(await p.evaluate(()=>Object.keys(sessionStorage).filter(k=>k.includes('.editor.'))),[]);
 const forbidden=await fetch(base+'/api/v1/projects/'+s.project+'/artifact-versions/'+s.version,{headers:{Authorization:'Bearer '+otherSession.token}});assert.equal(forbidden.status,404);await check('other_owner_cannot_restore_previous_draft_or_pending_request');await p.screenshot({path:out+'/03-other-account.png'});
 assert.deepEqual(errors,[]);await writeFile(out+'/result.json',JSON.stringify({passed:true,checks,errors,restored_version:committed.version.id,duplicate_versions:0,provider_posts:0},null,2));
}catch(e){await p.screenshot({path:out+'/failure.png'}).catch(()=>{});await writeFile(out+'/failure.txt',String(e));throw e;}finally{for(const session of sessions)cli('revoke-session','--session-id',session.session_id);await b.close();}
