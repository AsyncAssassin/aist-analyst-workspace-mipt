import {chromium} from 'playwright';
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {compose, webOrigin} from './local_compose.mjs';
import assert from 'node:assert/strict';
const root=process.cwd(),state=JSON.parse(await readFile('.runtime/private/state.json','utf8'));
const out=root+'/.runtime/evidence/browser';await mkdir(out,{recursive:true});
const base=webOrigin,path=`/v1/projects/${state.project}`,headers={Authorization:'Bearer '+state.session.token};
const api=async p=>{const r=await fetch(base+'/api'+p,{headers});assert.equal(r.status,200);return r.json();};
const browser=await chromium.launch({channel:'chrome',headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1050}}),page=await context.newPage();
page.setDefaultTimeout(15000);const errors=[],traffic=[],checks=[];page.on('pageerror',e=>errors.push(e.message));
page.on('response',r=>{if(r.url().includes('/api/'))traffic.push({path:new URL(r.url()).pathname,status:r.status(),method:r.request().method()});});
async function check(name){checks.push(name);console.log(name);await writeFile(out+'/progress.json',JSON.stringify({checks,traffic,errors},null,2));}
async function seed(p){await p.goto(base);await p.evaluate(s=>{const put=(k,v)=>sessionStorage.setItem('aist.d08.'+k,JSON.stringify(v));put('session',s.session.token);put('project',s.project);put('selection.'+s.project,{conversation:s.conversation,selected:[s.source],known:[s.source],run:s.run,result:s.version});},state);await p.reload();await p.getByRole('button',{name:'Редактировать записку'}).waitFor();}
async function saved(p,number){await p.locator('article.result-card').filter({has:p.getByText(`Версия ${number} ·`,{exact:false})}).waitFor();return p.locator('article.result-card').getAttribute('data-version-id');}
try{
 await seed(page);const v1=await api(path+'/artifact-versions/'+state.version);
 const existing=(await api(path+'/artifacts/'+state.artifact+'/versions')).items.find(v=>v.revision===2);
 if(!existing){
 await page.getByRole('button',{name:'Редактировать записку'}).click();
 await page.getByLabel('Подпись таблицы',{exact:true}).fill('Выручка ВУШ Холдинг, млн RUB; рост выручки, %; периоды по источнику');
 await page.getByRole('button',{name:'Добавить ручной абзац',exact:true}).first().click();
 const manual='Редакторская оговорка: выводы требуют обсуждения с командой.';
 const paragraphs=page.locator('.note-editor textarea');await paragraphs.nth(1).fill(manual);
 let dialog=false;page.once('dialog',async d=>{dialog=true;await d.dismiss();});await page.getByRole('button',{name:'К разговору',exact:true}).click();assert(dialog);
 assert.equal(await paragraphs.nth(1).inputValue(),manual);await check('unsaved_navigation_cancel');
 await page.getByRole('button',{name:'Сохранить новую версию',exact:true}).click();await saved(page,2);}
 else{await page.getByLabel('Сохраненные версии').selectOption(existing.id);}
 const v2id=await saved(page,2);const v2=await api(path+'/artifact-versions/'+v2id);
 assert.equal(v2.version.parent_version_id,state.version);assert.equal(v2.review.acceptance,null);assert.equal(v2.version.provenance.origin,'human_edit');
 assert.deepEqual(v2.version.basis,v1.version.basis);assert.deepEqual(v2.version.payload.content.sections[1].blocks[1].rows,v1.version.payload.content.sections[1].blocks[1].rows);
 assert.equal(v2.version.payload.content.sections[0].blocks[1].claim.origin,'human_editorial');
 assert.deepEqual((await api(path+'/artifact-versions/'+state.version)).version,v1.version);await check('v1_v2_parent_numbers_basis_unchanged');
 await page.screenshot({path:out+'/01-v2.png',fullPage:true});
 await page.getByRole('button',{name:'Основание: страница 11, 9M’21',exact:true}).first().click();
 await page.getByRole('dialog').waitFor();await page.screenshot({path:out+'/02-pdf.png'});await page.getByRole('button',{name:'Закрыть PDF',exact:true}).click();await check('source_pdf_open');
 await page.reload();await saved(page,2);
 compose('restart','backend','web');await new Promise(r=>setTimeout(r,2500));await page.reload();await saved(page,2);await check('reload_restart_v2');
 await page.getByRole('checkbox',{name:'Принимаю выбранную сохраненную версию без смысловой проверки'}).check();await page.getByRole('button',{name:'Принять выбранную версию',exact:true}).click();await page.getByText('Принята человеком:',{exact:false}).waitFor();
 const accepted=await api(path+'/artifact-versions/'+v2id);assert.equal(accepted.review.acceptance.content_sha256,v2.version.content_sha256);assert.equal(accepted.review.revision,1);await check('accept_v2');
 await page.getByRole('button',{name:'Редактировать записку'}).click();await page.getByLabel('Аудитория',{exact:true}).fill('Команда аналитиков');
 compose('stop','backend');await page.getByRole('button',{name:'Сохранить новую версию'}).click();await page.getByRole('button',{name:'Проверить сохранение'}).waitFor();assert.equal(await page.getByLabel('Аудитория',{exact:true}).inputValue(),'Команда аналитиков');await check('backend_failure_preserves_text');
 compose('start','--wait','backend');await page.getByRole('button',{name:'Проверить сохранение'}).click();const v3id=await saved(page,3);assert.equal((await api(path+'/artifact-versions/'+v3id)).review.acceptance,null);await check('new_version_has_no_acceptance');
 const second=await context.newPage();await seed(second);await second.getByLabel('Сохраненные версии').selectOption(v3id);await saved(second,3);
 await page.getByRole('button',{name:'Редактировать записку'}).click();await second.getByRole('button',{name:'Редактировать записку'}).click();
 await page.getByLabel('Аудитория',{exact:true}).fill('Первый редактор');await second.getByLabel('Аудитория',{exact:true}).fill('Второй редактор');
 await page.getByRole('button',{name:'Сохранить новую версию'}).click();const v4id=await saved(page,4);
 await second.getByRole('button',{name:'Сохранить новую версию'}).click();await second.getByRole('alert').filter({hasText:'Текст сохранен'}).waitFor();assert.equal(await second.getByLabel('Аудитория',{exact:true}).inputValue(),'Второй редактор');await second.screenshot({path:out+'/03-stale-tab.png',fullPage:true});await second.close();await check('two_tabs_conflict_keeps_text');
 await page.getByLabel('Сохраненные версии').selectOption(state.version);await saved(page,1);await page.getByRole('button',{name:'Продолжить от выбранной версии'}).click();await page.getByText('Продолжение от сохраненной v1.',{exact:false}).waitFor();await check('historical_version_selected_for_continuation');
 await page.getByRole('button',{name:'Открыть выбранный результат'}).click();assert.equal(await page.locator('article.result-card').getAttribute('data-version-id'),state.version);await check('return_keeps_conversation_and_selection');
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:out+'/04-mobile.png',fullPage:true});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await check('mobile_no_page_overflow');
 assert.deepEqual(errors,[]);
 await writeFile(out+'/result.json',JSON.stringify({passed:true,checks,traffic,errors,v1:state.version,v2:v2id,v3:v3id,v4:v4id},null,2));
 await writeFile(out+'/v2.json',JSON.stringify(v2,null,2));
}catch(e){await page.screenshot({path:out+'/failure.png',fullPage:true}).catch(()=>{});await writeFile(out+'/failure.txt',String(e)+'\n'+await page.locator('body').innerText());throw e;}
finally{compose('start','--wait','backend');await browser.close();}
