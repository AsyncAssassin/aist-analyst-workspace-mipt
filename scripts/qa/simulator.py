"""Explicit local HTTP simulator. No credentials and no upstream provider requests."""
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit,parse_qs

class State:
    def __init__(self):
        self.lock=threading.Lock();self.plans={};self.posts=[];self.aborts=[]
    def snapshot(self):return {'synthetic':True,'posts':self.posts,'post_count':len(self.posts),'aborts':self.aborts}

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    def send(self,value,status=200):
        raw=json.dumps(value,ensure_ascii=False).encode()
        try:self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError):pass
    def do_GET(self):
        path=urlsplit(self.path);q=parse_qs(path.query);s=self.server.state
        with s.lock:
            if path.path=='/stats':value=json.loads(json.dumps(s.snapshot()))
            elif path.path=='/plan':value={'steps':s.plans.get(q['run_id'][0],[{'kind':'model'},{'kind':'save'}])}
            elif path.path=='/abort':
                pair={'run_id':q['run_id'][0],'session_id':q['session_id'][0]}
                if pair not in s.aborts:s.aborts.append(pair)
                value={'aborted':True,**pair}
            else:return self.send({'error':'not_found'},404)
        self.send(value)
    def do_POST(self):
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<200000:return self.send({'error':'size'},400)
        v=json.loads(self.rfile.read(size));s=self.server.state
        if self.path=='/control':
            with s.lock:s.plans[v['run_id']]=v['steps']
            return self.send({'configured':True})
        if self.path!='/completion' or v.get('synthetic') is not True:return self.send({'error':'qa_only'},404)
        step=v['step']
        with s.lock:
            s.posts.append({'run_id':v['run_id'],'call_id':v['call_id'],'received_at':time.time()})
        time.sleep(step.get('delay',0))
        if step.get('disconnect'):
            self.close_connection=True;self.connection.shutdown(2);return
        status=step.get('status',200)
        reply={'synthetic':True,'id':'qa_'+v['call_id'],'usage':{'prompt_tokens':101,'completion_tokens':53,'cached_input':31,'reasoning_output':17}}
        if step.get('invalid'):reply={'synthetic':True,'invalid':True}
        self.send(reply,status)

if __name__=='__main__':
    server=ThreadingHTTPServer(('0.0.0.0',int(os.getenv('QA_PORT','8090'))),Handler);server.state=State();server.serve_forever()
