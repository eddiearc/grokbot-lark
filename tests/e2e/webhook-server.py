#!/usr/bin/env python3
"""Stand-in for the handler routine's webhook.

Usage: webhook-server.py <port file> <log file>
Listens on a free local port, writes the port number to <port file>, and appends one JSON line
per POST (Authorization header and JSON body) to <log file>.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT_FILE, LOG = sys.argv[1], sys.argv[2]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        with open(LOG, "a") as f:
            f.write(json.dumps({"authorization": self.headers.get("Authorization"), "body": body}) + "\n")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
with open(PORT_FILE, "w") as f:
    f.write(str(server.server_address[1]))
server.serve_forever()
