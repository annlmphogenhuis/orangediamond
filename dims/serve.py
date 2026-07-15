#!/usr/bin/env python3
"""
Local dev server with HTTP Range request support (required for video seeking).

Serves the BalanceCorpus root (the parent of this dims/ folder), so both
http://localhost:8001/dims/ and http://localhost:8001/viewer/ work. The served
root is derived from this file's location, so it works no matter the working
directory — including VSCode's "Run Python File" (play) button.

Usage: python serve.py [port]   (default port 8001)
"""
import http.server
import os
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # BalanceCorpus/


class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_head(self):
        path = self.translate_path(self.path.split('?')[0])

        if not os.path.isfile(path):
            return super().send_head()

        range_header = self.headers.get('Range')
        if not range_header:
            return super().send_head()

        size = os.path.getsize(path)
        try:
            byte_range = range_header.strip().removeprefix('bytes=')
            start_str, end_str = byte_range.split('-')
            start = int(start_str)
            end = int(end_str) if end_str else size - 1
        except (ValueError, AttributeError):
            self.send_error(400, 'Bad Range header')
            return None

        end = min(end, size - 1)
        length = end - start + 1

        f = open(path, 'rb')
        f.seek(start)

        self.send_response(206)
        self.send_header('Content-Type', self.guess_type(path))
        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()
        return f

    def log_message(self, fmt, *args):
        # Suppress noisy request logs; only show errors
        if args and str(args[1]) not in ('200', '206', '304'):
            super().log_message(fmt, *args)


class ReusableServer(http.server.HTTPServer):
    allow_reuse_address = True


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    url = f'http://localhost:{port}/dims/'
    try:
        server = ReusableServer(('', port), RangeRequestHandler)
    except OSError as e:
        if e.errno in (48, 98):  # Address already in use (macOS / Linux)
            print(f'Port {port} is already in use. Another server is likely '
                  f'still running.\nOpen {url} , or stop the old server with:\n'
                  f'    lsof -ti :{port} | xargs kill -9\n'
                  f'or run on a different port:  python serve.py {port + 1}')
            sys.exit(1)
        raise
    print(f'Serving {ROOT} on http://localhost:{port}')
    print(f'Dashboard: {url}')
    try:
        webbrowser.open(url)
    except Exception:
        pass
    server.serve_forever()
