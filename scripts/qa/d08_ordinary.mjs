import assert from 'node:assert/strict';
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const root=resolve(import.meta.dirname,'../..');
const {chromium}=await import(pathToFileURL(resolve(root,'.runtime/qa-tools/node_modules/playwright/index.mjs')));
const out=resolve(process.env.D08_EVIDENCE ?? root+'/.runtime/ordinary');await mkdir(out,{recursive:true});
const session=JSON.parse(await readFile(root+'/.runtime/private/sessions.json','utf8')).analyst;
const base='http://127.0.0.1:28911';
const request=async(path,body)=>fetch(base+'/api'+path,{method:body?'POST':'GET',headers:{Authorization:'Bearer '+session.token,...(body?{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()}:{})},body:body?JSON.stringify(body):undefined});
const browser=await chromium.launch({channel:'chrome',headless:true});const page=await browser.newPage({viewport:{width:1280,height:800},locale:'ru-RU'});
try{
 await page.goto(base);await page.getByLabel('Токен сессии').fill(session.token);await page.getByRole('button',{name:'Войти',exact:true}).click();await page.locator('.project-list button').first().click();await page.locator('.project-heading h1').waitFor();await page.locator('.conversation-picker option').filter({hasText:'Проверка источников'}).waitFor({state:'attached'});
 const d=page.locator('.conversation-picker details');if(!await d.evaluate(e=>e.open))await d.locator('summary').click();await page.getByLabel('Название разговора',{exact:true}).fill('Обычная сборка');const response=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/conversations'));await page.getByRole('button',{name:'Создать разговор',exact:true}).click();const conversation=await (await response).json();await page.getByText('Генерация пока недоступна',{exact:true}).waitFor();
 assert.equal(await page.locator('.qa-banner').count(),0);await page.getByRole('checkbox',{name:/^Версия 1 /}).check();await page.getByLabel('Задача по источникам',{exact:true}).fill('Черновик без запуска исполнителя.');assert.equal(await page.getByRole('button',{name:'Отправить задачу',exact:true}).isDisabled(),true);
 const project=conversation.project_id;const runsBefore=await (await request(`/v1/projects/${project}/runs`)).json();const source=(await (await request(`/v1/projects/${project}/sources`)).json()).items[0];const registry=await (await request('/v1/capabilities')).json();
 const result=await request(`/v1/projects/${project}/conversations/${conversation.id}/messages`,{expected_revision:conversation.revision,text:'Synthetic disabled probe',source_version_ids:[source.latest_version_id],skill_version_id:'skill_qa-d07-v1',model_profile_version_id:'model_qa-d07-v1',requested_result_type:'evidence_answer',parameters:{audience:null,sections:[]},base_version_id:null,expected_artifact_revision:0,requested_export:null});const rejected=await result.json();assert.equal(result.status,409);assert.equal(rejected.error.code,'tool_unavailable');
 const runsAfter=await (await request(`/v1/projects/${project}/runs`)).json();assert.equal(runsAfter.items.length,runsBefore.items.length);await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:out+'/ordinary.png',fullPage:false});
 await writeFile(out+'/ordinary.json',JSON.stringify({passed:true,qa_banner:false,submit_disabled:true,probe_status:409,probe_error:'tool_unavailable',runs_before:runsBefore.items.length,runs_after:runsAfter.items.length,project_id:project,conversation_id:conversation.id,registry_status:registry.skills.map(s=>({id:s.id,status:s.status}))},null,2));console.log('PASS ordinary disabled UI and server, no new Run');
}catch(e){await page.screenshot({path:out+'/failure.png'}).catch(()=>{});throw new Error(String(e.stack??e).replaceAll(session.token,'[redacted]'));}finally{await browser.close();}
