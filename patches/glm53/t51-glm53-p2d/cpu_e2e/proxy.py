"""Minimal P/D proxy for the CPU test, standard library only: send the request to P with max_tokens=1 and
stream=false (P computes and saves the KV through ExampleConnector), then forward the original request to D and
relay D's response. Usage: python3 proxy.py <listen port> <P base url> <D base url>
"""

import http.client
import http.server
import json
import socketserver
import sys
import urllib.parse

port, p_url, d_url = int(sys.argv[1]), urllib.parse.urlparse(sys.argv[2]), urllib.parse.urlparse(sys.argv[3])


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        conn = http.client.HTTPConnection(d_url.hostname, d_url.port, timeout=60)
        conn.request("GET", self.path)
        resp = conn.getresponse()
        data = resp.read()
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.getheader("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        request = json.loads(raw)
        prefill = dict(request, max_tokens=1, stream=False, n=1)
        prefill.pop("stream_options", None)
        conn = http.client.HTTPConnection(p_url.hostname, p_url.port, timeout=300)
        conn.request("POST", self.path, json.dumps(prefill), {"Content-Type": "application/json"})
        conn.getresponse().read()
        conn.close()
        conn = http.client.HTTPConnection(d_url.hostname, d_url.port, timeout=300)
        conn.request("POST", self.path, raw, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.send_response(resp.status)
        stream = request.get("stream")
        self.send_header("Content-Type", resp.getheader("Content-Type", "application/json"))
        if stream:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client went away: closing D's connection makes D abort the request
            finally:
                conn.close()
            return
        data = resp.read()
        conn.close()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


Server(("0.0.0.0", port), Handler).serve_forever()
