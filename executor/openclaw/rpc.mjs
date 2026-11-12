import { t as GatewayClient } from '/opt/aist-openclaw/tools/node-v24.21.0/lib/node_modules/openclaw/dist/client-3bxKJdYL.mjs';
process.stdin.setEncoding('utf8');
let raw = ''; for await (const chunk of process.stdin) raw += chunk;
const {port, method, params} = JSON.parse(raw);
let resolveHello, rejectHello;
const hello = new Promise((resolve, reject) => { resolveHello=resolve; rejectHello=reject; });
const client = new GatewayClient({url:`ws://127.0.0.1:${port}`, token:process.env.OPENCLAW_GATEWAY_TOKEN,
  clientName:'cli',mode:'cli',scopes:['operator.admin'],requestTimeoutMs:30000,
  onHelloOk:resolveHello,onConnectError:rejectHello});
let timer;
const deadline=new Promise((_,reject)=>{timer=setTimeout(()=>{reject(Error('RPC deadline'));client.stop();},60000);});
try {
  const result=await Promise.race([(async()=>{client.start();await hello;return client.request(method,params);})(),deadline]);
  process.stdout.write(JSON.stringify(result));
}
catch {process.stderr.write('Installed Gateway RPC failed\n');process.exitCode=1;}
finally {clearTimeout(timer);client.stop();}
