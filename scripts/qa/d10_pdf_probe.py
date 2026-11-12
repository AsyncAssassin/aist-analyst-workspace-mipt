import subprocess,sys,time,json
start=time.monotonic();data=sys.stdin.buffer.read()
p=subprocess.run([sys.executable,'-m','app.pdf_worker'],input=data,capture_output=True,timeout=30)
try:body=json.loads(p.stdout)
except Exception:body={}
print(json.dumps({'bytes':len(data),'returncode':p.returncode,'elapsed':time.monotonic()-start,'stdout_bytes':len(p.stdout),'error':body.get('error'),'pages':len(body.get('pages',[]))}))
