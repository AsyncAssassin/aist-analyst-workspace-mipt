import {chromium} from 'playwright';
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import assert from 'node:assert/strict';
const s=JSON.parse(await readFile('.runtime/private/state.json','utf8')),base='http://127.0.0.1:29411',root=`/v1/projects/${s.project}`;
const out='.runtime/evidence/browser-final';await mkdir(out,{recursive:true});
const b=await chromium.launch({channel:'chrome',headless:true}),p=await b.newPage({viewport:{width:1440,height:1050}});p.setDefaultTimeout(15000);
const checks=[],errors=[];p.on('pageerror',e=>errors.push(e.message));
const api=async path=>{const r=await fetch(base+'/api'+path,{headers:{Authorization:'Bearer '+s.session.token}});assert.equal(r.status,200);return r.json();};
async function check(name){checks.push(name);console.log(name);}
try{
 await p.goto(base);await p.evaluate(s=>{const put=(k,v)=>sessionStorage.setItem('aist.d08.'+k,JSON.stringify(v));put('session',s.session.token);put('project',s.project);put('selection.'+s.project,{conversation:s.conversation,selected:[s.source],known:[s.source],run:s.run,result:s.version});},s);await p.reload();
 await p.getByRole('button',{name:'Редактировать записку'}).click();
 await p.getByRole('button',{name:'Добавить ручной абзац'}).first().click();await p.getByLabel('Абзац',{exact:true}).nth(1).fill('Временный абзац редактора.');
 await p.getByRole('button',{name:'Удалить ручной абзац'}).click();assert.equal(await p.getByRole('button',{name:'Удалить ручной абзац'}).count(),0);await check('add_delete_manual_paragraph');
 await p.getByLabel('Аудитория',{exact:true}).fill('Черновик остается после обновления страницы');
 p.once('dialog',d=>d.dismiss());await p.getByRole('button',{name:'К разговору',exact:true}).click();assert.equal(await p.getByLabel('Аудитория',{exact:true}).inputValue(),'Черновик остается после обновления страницы');await check('unsaved_navigation_warning');
 p.once('dialog',d=>d.accept());await p.reload();await p.getByRole('button',{name:'Редактировать записку'}).click();assert.equal(await p.getByLabel('Аудитория',{exact:true}).inputValue(),'Черновик остается после обновления страницы');await check('unsaved_reload_restores_draft');
 // Только ответ о сессии подменен для проверки клиентской ошибки. Реальная сессия не отзывается.
 await p.route('**/api'+root+'/artifacts/'+s.artifact+'/versions',route=>route.fulfill({status:401,contentType:'application/json',body:JSON.stringify({error:{code:'unauthenticated'}})}));
 await p.getByRole('button',{name:'Сохранить новую версию'}).click();await p.getByRole('alert').filter({hasText:'Сессия'}).waitFor();assert.equal(await p.getByLabel('Аудитория',{exact:true}).inputValue(),'Черновик остается после обновления страницы');await check('auth_error_keeps_editor_text');await p.unroute('**/api'+root+'/artifacts/'+s.artifact+'/versions');
 await p.screenshot({path:out+'/01-editor.png'});
 await p.getByRole('button',{name:'Войти с новой сессией'}).click();await p.getByLabel('Токен сессии').fill(s.session.token);await p.getByRole('button',{name:'Войти',exact:true}).click();await p.getByRole('button',{name:'Редактировать записку'}).click();
 await p.getByRole('button',{name:'Проверить сохранение'}).click();await p.getByRole('button',{name:'Редактировать записку'}).waitFor();await p.waitForFunction(id=>document.querySelector('.result-card')?.getAttribute('data-version-id')!==id,s.version);
 const savedId=await p.locator('.result-card').getAttribute('data-version-id'),saved=await api(root+'/artifact-versions/'+savedId);assert.equal(saved.version.parent_version_id,s.version);assert.equal(saved.review.acceptance,null);await check('final_build_saves_historical_parent');
 await p.getByRole('button',{name:'Редактировать записку'}).click();await p.getByLabel('Аудитория',{exact:true}).fill('Эту несохраненную форму исполнитель не получит');p.once('dialog',d=>d.accept());await p.getByRole('button',{name:'Продолжить от выбранной версии'}).click();
 const selection=await p.evaluate(id=>JSON.parse(sessionStorage.getItem('aist.d08.base.'+id)),s.conversation);assert.equal(selection.id,savedId);assert.equal(selection.revision,saved.version.revision);
 // Включаем только видимость принятого registry, исходящий submit перехватывается до backend.
 const registry=JSON.parse(await readFile('contracts/v1/registry.json','utf8'));const skill=registry.skills.find(x=>x.id==='skill_analytical-note-v1');skill.status='enabled';skill.bundle_sha256='1'.repeat(64);registry.model_profiles[0].status='enabled';registry.model_profiles[0].configuration_sha256='2'.repeat(64);
 await p.route('**/api/v1/capabilities',r=>r.fulfill({status:200,contentType:'application/json',body:JSON.stringify(registry)}));await p.reload();
 let submitted;await p.route('**/api'+root+'/conversations/'+s.conversation+'/messages',async r=>{if(r.request().method()==='POST'){submitted=r.request().postDataJSON();await r.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:{code:'temporarily_unavailable'}})});}else await r.continue();});
 await p.getByLabel('Задача по источникам',{exact:true}).fill('Продолжить выбранную сохраненную версию');await p.getByRole('button',{name:'Отправить задачу',exact:true}).click();await p.getByRole('button',{name:'Проверить исход отправки'}).waitFor();
 assert.equal(submitted.base_version_id,savedId);assert.equal(submitted.expected_artifact_revision,saved.version.revision);assert(!JSON.stringify(submitted).includes('Эту несохраненную форму'));await check('ui_submit_uses_saved_id_revision_without_draft');
 await p.screenshot({path:out+'/02-continuation.png'});
 assert.deepEqual(errors,[]);await writeFile(out+'/result.json',JSON.stringify({passed:true,checks,errors,submit:submitted,saved_version:savedId,provider_posts:0},null,2));
}catch(e){await p.screenshot({path:out+'/failure.png'}).catch(()=>{});await writeFile(out+'/failure.txt',String(e));throw e;}finally{await b.close();}
