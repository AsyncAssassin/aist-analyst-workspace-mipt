import assert from 'node:assert/strict';
import {mkdtemp,copyFile,writeFile,rm} from 'node:fs/promises';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import test from 'node:test';

test('Вложенное извлечение ожидает модель дольше срока локального чтения и поддерживает отмену', {timeout:25000}, async()=>{
  const directory=await mkdtemp(path.join(os.tmpdir(),'aist-tool-'));
  const socketPath=path.join(directory,'tools.sock');
  await copyFile(new URL('../../executor/openclaw/plugin/index.mjs',import.meta.url),path.join(directory,'index.mjs'));
  await writeFile(path.join(directory,'schemas.json'),JSON.stringify({aist_get_facts:{type:'object'}}));
  const pending=new Set();
  const server=net.createServer(socket=>{
    socket.once('data',data=>{
      const request=JSON.parse(data.toString());
      const timer=setTimeout(()=>{pending.delete(timer);socket.end(JSON.stringify({ok:true,result:{facts:[]}})+'\n');},request.args.extract?16000:1);
      pending.add(timer);
      socket.once('close',()=>{clearTimeout(timer);pending.delete(timer);});
    });
  });
  await new Promise(resolve=>server.listen(socketPath,resolve));
  const oldSocket=process.env.AIST_TOOLS_SOCKET,oldTimeout=process.env.AIST_EXTRACTION_TIMEOUT_MS;
  process.env.AIST_TOOLS_SOCKET=socketPath;process.env.AIST_EXTRACTION_TIMEOUT_MS='20000';
  try{
    const plugin=(await import(pathToFileURL(path.join(directory,'index.mjs')))).default;
    let factory;
    plugin.register({registerTool(value){factory=value;}});
    const tool=factory.create({sessionKey:'synthetic-session',assertInvocationCurrent(){}});
    const args={refs:[],extract:{source_version_id:'sv_synthetic',question:'Описание бизнеса'}};
    const result=await tool.execute('call_synthetic',args,new AbortController().signal);
    assert.deepEqual(result.details,{ok:true,result:{facts:[]}});
    const controller=new AbortController();
    const cancelled=tool.execute('call_cancelled',args,controller.signal);
    setTimeout(()=>controller.abort(),20);
    await assert.rejects(cancelled,/aborted/);
  }finally{
    if(oldSocket===undefined)delete process.env.AIST_TOOLS_SOCKET;else process.env.AIST_TOOLS_SOCKET=oldSocket;
    if(oldTimeout===undefined)delete process.env.AIST_EXTRACTION_TIMEOUT_MS;else process.env.AIST_EXTRACTION_TIMEOUT_MS=oldTimeout;
    for(const timer of pending)clearTimeout(timer);
    await new Promise(resolve=>server.close(resolve));
    await rm(directory,{recursive:true,force:true});
  }
});
