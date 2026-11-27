#!/usr/bin/env python3
"""Loopback-only webhook receiver; Ansible owns its lifecycle and configuration."""
import argparse
import http.server
import json
import os
from pathlib import Path
import time


def serve(directory, run_id, lifetime):
    directory = Path(directory)
    received = set()
    state = directory / 'receiver.json'

    def save(port):
        temporary = directory / 'receiver.json.tmp'
        temporary.write_text(json.dumps({'port': port, 'sources': sorted(received)}))
        os.replace(temporary, state)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.connection.settimeout(5)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if self.path != '/' + run_id or not 0 < size <= 1048576:
                    raise ValueError('Invalid request')
                payload = json.loads(self.rfile.read(size))
                if payload.get('receiver') != 'ODVValidation_' + run_id:
                    raise ValueError('Unexpected receiver')
                for alert in payload.get('alerts', []):
                    labels = alert.get('labels', {})
                    if (alert.get('status') == 'firing' and labels.get('odv_run_id') == run_id
                            and labels.get('alertname') == 'ODVValidation_' + run_id
                            and isinstance(labels.get('odv_source'), str)):
                        received.add(labels['odv_source'])
                save(self.server.server_port)
                self.send_response(200)
            except (ValueError, TypeError, AttributeError, OSError):
                self.send_response(400)
            self.end_headers()

        def log_message(self, *args):
            pass

    with http.server.HTTPServer(('127.0.0.1', 0), Handler) as server:
        server.timeout = 0.5
        save(server.server_port)
        deadline = time.monotonic() + lifetime
        while not (directory / 'stop').exists() and time.monotonic() < deadline:
            server.handle_request()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory')
    parser.add_argument('run_id')
    parser.add_argument('--lifetime', type=int, default=7200)
    args = parser.parse_args()
    serve(args.directory, args.run_id, args.lifetime)
