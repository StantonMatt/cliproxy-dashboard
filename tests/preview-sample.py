#!/usr/bin/env python3
"""Bounded phone-check fixture. Serves only this page; never browses files."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

PAGE = b'''<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Linux preview check</title>
<style>body{max-width:520px;margin:60px auto;padding:0 24px;background:#0b0e13;color:#edf1f7;font:16px system-ui}h1{font-size:27px}p{color:#8c98aa;line-height:1.6}button{border:0;border-radius:10px;padding:14px 20px;background:#87c3ff;color:#0b0e13;font:inherit;cursor:pointer}#result{color:#83d8be}</style>
<h1>Linux preview</h1><p>This page is running on your Linux computer.</p><button id="check">Check connection</button><p id="result" role="status"></p>
<script>document.querySelector('#check').onclick=async()=>{try{const r=await fetch('/check',{method:'POST'});const d=await r.json();document.querySelector('#result').textContent=d.ok?'Connection confirmed.':'Check failed.';}catch{document.querySelector('#result').textContent='Connection unavailable.';}};</script></html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        body = PAGE if self.path == '/' else b'Not found'
        self.send_response(200 if self.path == '/' else 404)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.dumps({'ok': self.path == '/check'}).encode()
        self.send_response(200 if self.path == '/check' else 404)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(('127.0.0.1', 8421), Handler)
timer = threading.Timer(1800, server.shutdown)
timer.daemon = True
timer.start()
try:
    server.serve_forever()
finally:
    timer.cancel()
    server.server_close()
