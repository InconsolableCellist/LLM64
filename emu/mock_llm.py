#!/usr/bin/env python3
"""Mock OpenAI-compatible API server for automated tests.

Speaks just enough of the /v1/chat/completions SSE streaming protocol to
stand in for a real LLM backend, with deterministic canned responses so
test assertions are stable. Stdlib only.
"""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_RESPONSE = "Hello from the mock LLM! Greetings, Commodore 64."

# Requested by sending a message containing LONGTEST: exercises sustained
# streaming (M2 serial driver acceptance).
LONG_RESPONSE = " ".join(
    f"Sentence number {i} of the long streaming test." for i in range(1, 61)
)

CHUNK_SIZE = 8


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        print(f"[mock_llm] {fmt % args}", file=sys.stderr)

    def do_GET(self):
        if self.path.rstrip('/').endswith('/models'):
            body = json.dumps({
                'object': 'list',
                'data': [{'id': 'mock', 'object': 'model'},
                         {'id': 'mock-large', 'object': 'model'}],
            }).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.path.rstrip('/').endswith('/chat/completions'):
            self.send_error(404)
            return

        length = int(self.headers.get('Content-Length', 0))
        request = json.loads(self.rfile.read(length) or b'{}')

        user_text = ''
        for msg in request.get('messages', []):
            if msg.get('role') == 'user':
                user_text = msg.get('content', '')

        upper = user_text.upper()
        if 'STALLTEST' in upper:
            # Accept the request but never stream anything: exercises the
            # client's response watchdog (a request that gets no reply).
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            import time as _t
            _t.sleep(120)
            return
        if 'CURRENT SCENE FOR AN ILLUSTRATOR' in upper:
            # Scene composition (docs/13): /pic now asks the model to WRITE
            # the image prompt from game state instead of sending a
            # directive verbatim, so the derive step lands here. Return a
            # clean, deterministic scene so the stored prompt (and /pics)
            # stays stable. Must precede PICTEST - the transcript embedded
            # in this question contains 'pictest'.
            text = ('a vast crystal cavern, stalactites glinting above a '
                    'black lake')
        elif 'LONGTEST' in upper:
            text = LONG_RESPONSE
        elif 'PARAMTEST' in upper:
            # Echo the sampling parameters so tests can assert they arrived
            text = ('params: temp {} topk {} topp {}'.format(
                request.get('temperature'), request.get('top_k'),
                request.get('top_p')))
        elif 'PICTEST' in upper:
            # Image directive with a description long enough to split
            # across chunk boundaries
            text = ('[[IMAGE: a vast crystal cavern, stalactites glinting '
                    'above a black lake]] The cavern opens before you, '
                    'glittering endlessly.')
        elif 'MUSICTEST' in upper:
            # Music directive split mid-token across SSE chunks (CHUNK_SIZE
            # boundaries land inside [[MUSIC:...]]): exercises the proxy's
            # hold-back stripping
            text = ('[[MUSIC: festive]] The carnival begins! Lanterns '
                    'bob between the stalls.')
        elif 'COLORTEST' in upper:
            # Colour markup split across SSE chunk boundaries, so the
            # proxy's hold-back has to reassemble a tag before the
            # marker transform runs. Both a colour run and **bold**.
            text = ('You approach the [color=grey]steel door[/color], '
                    'your torch guttering. Go **north** now.')
        elif 'ATMOSPHERIC CAPTION' in upper:
            # Deterministic caption: the e2e converter probe bakes the
            # same text into its expected caption band
            text = 'The crystal deep hums with cold light.'
        elif 'BEGIN THE ADVENTURE' in upper:
            # Status bar (visible) + [[STATE]] block (stripped + saved
            # to meta) - the e2e asserts both behaviors
            text = ('[HP 10/10 | The Dark Room]\n'
                    'You awaken in a dark room smelling of ozone. A single '
                    'door stands to the north. Your quest awaits.\n'
                    '[[STATE: {"hp":10,"maxhp":10,"location":"dark room",'
                    '"inventory":[],"appearance":"a wiry traveler in a '
                    'patched gray cloak","companions":[]}]]')
        else:
            text = DEFAULT_RESPONSE

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()

        for i in range(0, len(text), CHUNK_SIZE):
            chunk = {
                'choices': [{'delta': {'content': text[i:i + CHUNK_SIZE]}}],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser(description='Mock OpenAI SSE server')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=0,
                        help='Port to listen on (0 = ephemeral, printed to stdout)')
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    # Announce the actual port so the harness can use --port 0
    print(f"MOCK_LLM_PORT={server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
