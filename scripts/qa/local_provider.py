"""Локальная проверка развертывания: штатный worker с синтетическим Responses HTTP."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from app import openai_responses, transport
from app.config import Settings
from app.worker import Worker


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert self.path == '/v1/responses'
        assert self.headers['Authorization'] == 'Bearer synthetic-local-only'
        assert request['model'] == 'gpt-6-astra'
        assert request['reasoning'] == dict(effort='xhigh', mode='standard')
        assert request['service_tier'] == 'default'
        assert request['store'] is False
        response = dict(
            id='resp_local', object='response', model='gpt-6-astra', status='completed', error=None,
            service_tier='default', reasoning=dict(effort='xhigh', mode='standard'),
            output=[dict(type='function_call', id='fc_local', call_id='call_local',
                         name='aist_save_artifact', status='completed',
                         arguments=json.dumps(dict(content=dict(claims=[], missing_information=[
                             'Локальная проверка выполнена. Ответ синтетический, внешняя модель не вызывалась.'
                         ])), ensure_ascii=False))],
            usage=dict(input_tokens=100, output_tokens=20, total_tokens=120,
                       input_tokens_details=dict(cached_tokens=0, cache_write_tokens=0),
                       output_tokens_details=dict(reasoning_tokens=0)),
        )
        data = openai_responses.to_sse(response).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        print('Synthetic Responses request completed: gpt-6-astra/xhigh/default', flush=True)


def main():
    if openai_responses.credential(Settings().openai_secret_path) != 'synthetic-local-only':
        raise SystemExit('This check accepts only the synthetic-local-only credential')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    transport.ReaderHTTPSConnection = lambda *args, **kwargs: transport.ReaderConnection(
        '127.0.0.1', server.server_port, timeout=3)
    try:
        return Worker().run()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    raise SystemExit(main())
