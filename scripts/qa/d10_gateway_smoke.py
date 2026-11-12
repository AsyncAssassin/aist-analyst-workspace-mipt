"""Installed Gateway/plugin startup only, no agent or provider invocation."""
import json,secrets,socket,subprocess,time,sys,os,signal
from pathlib import Path
from app.config import Settings
from app.openclaw_runtime import prepare,environment,Gateway,CLI
s=Settings();d=Path(s.openclaw_root)/(sys.argv[1] if len(sys.argv)>1 else 'gateway-startup-smoke');d.mkdir(mode=0o700)
token=secrets.token_hex(32);prepare(s,d,token);env=environment(d,token)
with (d/'process.log').open('w') as log:
 p=subprocess.Popen([CLI,'gateway','run'],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 try:
  end=time.monotonic()+150
  while True:
   if p.poll() is not None:raise RuntimeError('Gateway exited')
   try:
    with socket.create_connection(('127.0.0.1',s.gateway_port),timeout=.2):break
   except OSError:
    if time.monotonic()>end:raise RuntimeError('Gateway readiness timeout')
    time.sleep(.2)
  reply=Gateway(s,d,env).request('health',{})
  (d/'summary.json').write_text(json.dumps({'passed':True,'model_posts':0,'rpc_health':reply},indent=2)+'\n')
  print('Installed Gateway and plugins ready; no agent request and no model POST')
 finally:
  os.killpg(p.pid,signal.SIGTERM)
  try:p.wait(timeout=5)
  except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
