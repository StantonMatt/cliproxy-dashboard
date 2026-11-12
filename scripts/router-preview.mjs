#!/usr/bin/env node
// Explicitly registered loopback previews and dashboard controls for approved private devices.
import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';
import path from 'node:path';
import net from 'node:net';
import fs from 'node:fs';

const ACCOUNT_MUTATION_DEADLINE_MS = 45000;
const ACCOUNT_PROXY_TIMEOUT_MS = ACCOUNT_MUTATION_DEADLINE_MS + 5000;
const ACCOUNT_MUTATION_PATHS = ['/api/accounts/signin/start','/api/accounts/signin/cancel','/api/accounts/add/start','/api/accounts/add/callback','/api/accounts/add/cancel','/api/accounts/disconnect','/api/accounts/api-keys/add','/api/accounts/routing','/api/accounts/usage/refresh','/api/routing','/api/claude-routing','/api/routing-link','/api/routing-all','/api/claude/accounts/routing'];

const configPath = process.env.ROUTER_PREVIEW_CONFIG || '/etc/personal-codex-router/previews.json';
const config = () => JSON.parse(fs.readFileSync(configPath, 'utf8'));
const initial = config();
const dashboardTarget = Number(process.env.ROUTER_PREVIEW_DASHBOARD_PORT || 8320);
const credential = name => path.join(process.env.CREDENTIALS_DIRECTORY || '', name);
const tlsOptions = () => ({
  cert:fs.readFileSync(process.env.ROUTER_PREVIEW_CERT || credential('preview.crt')),
  key:fs.readFileSync(process.env.ROUTER_PREVIEW_KEY || credential('preview.key')),
  minVersion:'TLSv1.2',
});
let credentials = tlsOptions();
tls.createSecureContext(credentials); // Fail closed before opening any listeners.
const servers = [];
const secureServers = [];
const externalHost = c => c.tls_hostname || c.dns;
const externalOrigin = (c, port) => `https://${externalHost(c)}:${port}`;
const scheme = req => req.socket.encrypted ? 'https' : 'http';
process.on('SIGHUP',()=>{
  try {
    const next=tlsOptions();tls.createSecureContext(next);
    for(const server of secureServers)server.setSecureContext(next);
    credentials=next;
  } catch { console.error('Preview certificate reload failed; retaining the current certificate.'); }
});
const connections = new Set();
const track = (req, port, p, close) => {
  const entry={req,port,p,close};connections.add(entry);
  return ()=>connections.delete(entry);
};
setInterval(()=>{
  let c;try{c=config();}catch{for(const e of connections)e.close();return;}
  for(const e of connections) {
    const p=route(e.port,c);
    if(!p || p.target!==e.p.target || p.host!==e.p.host || p.name!==e.p.name || !permitted(e.req,e.port,c))e.close();
  }
},1000).unref();
const escape = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const respond = (res, code, body) => {
  res.writeHead(code, {'content-type':'text/html; charset=utf-8','cache-control':'no-store',
    'x-content-type-options':'nosniff','referrer-policy':'no-referrer','x-frame-options':'DENY',
    'content-security-policy':"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"});
  res.end(body);
};
function permitted(req, port, c) {
  const source = req.socket.remoteAddress?.replace(/^::ffff:/,'');
  if (![c.bind,...c.devices.flatMap(d=>d.ips)].includes(source)) return false;
  if (![c.hostname,c.dns,c.tls_hostname,c.bind,...(c.aliases || [])].some(h=>req.headers.host===`${h}:${port}`)) return false;
  const userNavigation = req.method==='GET' && req.headers['sec-fetch-mode']==='navigate' &&
    req.headers['sec-fetch-dest']==='document' && req.headers['sec-fetch-user']==='?1';
  if(req.headers['sec-fetch-site']==='cross-site' && !userNavigation) return false;
  if(req.headers.origin && req.headers.origin!==`${scheme(req)}://${req.headers.host}`) return false;
  return true;
}
const expiresAt = p => p.permanent===true ? Infinity : (p.expires_at || 0)*1000;
const route = (port,c) => port===c.base_port+9 ?
  {name:'Personal Router',target:dashboardTarget,host:'127.0.0.1:8320',dashboard:true} :
  c.previews.find(p=>p.listen===port && expiresAt(p)>Date.now());
function dashboardRequestAllowed(req) {
  const path=req.url.split('?')[0];
  if(req.method==='GET')return ['/', '/index.html', '/app.js', '/billing.js', '/style.css', '/fonts.css',
    '/favicon.svg', '/boards.js', '/boards.css', '/charts.js', '/widgets.js', '/charts.css',
    '/api/series', '/api/breakdown', '/api/heatmap', '/api/retry', '/api/overview', '/api/requests', '/api/accounts/add/status', '/api/health', '/ibm-plex-serif-0.ttf'].includes(path) || /^\/(manrope-[0-4]|ibm-plex-mono-[01])\.ttf$/.test(path);
  return req.method==='POST' && ['/api/claude-warnings/dismiss','/api/accounts/billing','/api/layout','/api/refresh','/api/routing','/api/claude-routing','/api/routing-link','/api/routing-all','/api/claude/accounts/routing','/api/resets/policy','/api/resets/consume','/api/claude/resets/consume','/api/claude/resets/policy','/api/fast-mode','/api/accounts/signin/start','/api/accounts/signin/cancel','/api/accounts/add/start','/api/accounts/add/callback','/api/accounts/add/cancel','/api/accounts/disconnect','/api/accounts/api-keys/add','/api/accounts/routing','/api/accounts/usage/refresh'].includes(path);
}
function upstreamTimeout(req, p) {
  if(p.dashboard && ACCOUNT_MUTATION_PATHS.includes(req.url.split('?')[0]))return ACCOUNT_PROXY_TIMEOUT_MS;
  return p.dashboard && ['/api/claude/resets/consume','/api/claude/resets/policy'].includes(req.url.split('?')[0]) ? 65000 : 30000;
}
function outgoing(req, p, upgrade=false) {
  const headers = {...req.headers,host:p.host};
  // Never trust client forwarding metadata or pass proxy credentials upstream.
  for(const name of Object.keys(headers)) {
    if(name.startsWith('x-forwarded-') || name.startsWith('tailscale-') ||
       ['forwarded','proxy-authorization','proxy-connection'].includes(name)) delete headers[name];
  }
  if(!upgrade) {
    for(const name of String(req.headers.connection || '').toLowerCase().split(',')) delete headers[name.trim()];
    for(const name of ['connection','keep-alive','te','trailer','transfer-encoding','upgrade']) delete headers[name];
    headers.connection='close';
  }
  headers['x-forwarded-proto']='https';
  if(headers.origin) headers.origin=`http://${p.host}`;
  delete headers['x-router-read-only'];
  delete headers['x-router-transport'];
  if(p.dashboard)headers['x-router-transport']='tailscale';
  return headers;
}
function portal(c) {
  const entries=c.previews.filter(p=>expiresAt(p)>Date.now());
  return `<!doctype html><html lang="en"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Private previews</title><style>body{margin:48px auto;padding:0 24px;max-width:600px;background:#0b0e13;color:#edf1f7;font:16px system-ui}h1{font-size:26px}a{display:block;padding:20px 0;color:#87c3ff;text-decoration:none}small{display:block;color:#8c98aa;margin-top:6px}p{color:#8c98aa}</style><a href="https://${escape(externalHost(c))}:${c.base_port+9}/">Personal Router ↗</a><h1>Previews</h1>${entries.length?entries.map(p=>`<a href="https://${escape(externalHost(c))}:${p.listen}/">${escape(p.name)} ↗<small>${p.permanent===true?'Permanent':`Until ${escape(new Date(p.expires_at*1000).toISOString().replace('T',' ').slice(0,16))} UTC`}</small></a>`).join(''):'<p>No active previews.</p>'}</html>`;
}
for (let port=initial.base_port; port<initial.base_port+10; port++) {
  const server=https.createServer({...credentials,handshakeTimeout:15000,connectionsCheckingInterval:1000},(req,res)=>{
    let c;try{c=config();}catch{return respond(res,503,'Preview configuration unavailable.');}
    if(!permitted(req,port,c))return respond(res,403,'Private device access required.');
    if(!req.url.startsWith('/') || req.url.startsWith('//'))return respond(res,400,'Invalid request.');
    if(port===c.base_port)return respond(res,req.method==='GET'?200:405,portal(c));
    const p=route(port,c);if(!p)return respond(res,404,'Preview ended.');
    if(p.dashboard && !dashboardRequestAllowed(req))return respond(res,403,'Unknown dashboard endpoint.');
    const upstream=http.request({host:'127.0.0.1',port:p.target,method:req.method,path:req.url,
      headers:outgoing(req,p),timeout:upstreamTimeout(req,p)},reply=>{
      const headers={...reply.headers};
      delete headers.connection;delete headers['transfer-encoding'];
      // Relative redirects work naturally; rewrite only this upstream origin.
      if(headers.location) {
        try {
          const location=new URL(headers.location);
          const local=['http','https'].flatMap(scheme=>[`${scheme}://${p.host}`,`${scheme}://${new URL(`http://${p.host}`).hostname}:${p.target}`,
            `${scheme}://localhost:${p.target}`,`${scheme}://127.0.0.1:${p.target}`]);
          if(local.includes(location.origin) && !location.username && !location.password)
            headers.location=externalOrigin(c,port)+location.pathname+location.search+location.hash;
        } catch { /* Relative locations already use the external origin. */ }
      }
      if(headers['set-cookie'])headers['set-cookie']=headers['set-cookie'].map(cookie=>
        cookie.replace(/;\s*Domain=\.?localhost\s*(?=;|$)/ig,''));
      res.writeHead(reply.statusCode,headers);reply.pipe(res);
      reply.on('error',()=>res.destroy());
    });
    const accountDeadline=p.dashboard && ACCOUNT_MUTATION_PATHS.includes(req.url.split('?')[0]) ?
      setTimeout(()=>upstream.destroy(),upstreamTimeout(req,p)) : null;
    const untrack=track(req,port,p,()=>{upstream.destroy();res.destroy();});
    upstream.on('timeout',()=>upstream.destroy());
    upstream.on('error',()=>{if(!res.headersSent)respond(res,502,'Development server unavailable.');else res.destroy();});
    res.on('close',()=>{untrack();clearTimeout(accountDeadline);upstream.destroy();});
    req.on('aborted',()=>upstream.destroy());
    req.pipe(upstream);
  });
  server.on('upgrade',(req,socket,head)=>{
    let c;try{c=config();}catch{socket.destroy();return;}
    const p=route(port,c);
    if(!permitted(req,port,c) || !p || p.dashboard || !req.url.startsWith('/') || req.url.startsWith('//') || req.headers.upgrade?.toLowerCase()!=='websocket') {
      socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n');return;
    }
    const upstream=net.connect({host:'127.0.0.1',port:p.target});
    const untrack=track(req,port,p,()=>{socket.destroy();upstream.destroy();});
    upstream.setTimeout(120000,()=>upstream.destroy());
    socket.setTimeout(120000,()=>socket.destroy());
    upstream.on('connect',()=>{
      upstream.write(`${req.method} ${req.url} HTTP/1.1\r\n`+Object.entries(outgoing(req,p,true)).map(([k,v])=>`${k}: ${v}`).join('\r\n')+'\r\n\r\n');
      if(head.length)upstream.write(head);
      socket.pipe(upstream).pipe(socket);
    });
    upstream.on('error',()=>socket.destroy());socket.on('error',()=>upstream.destroy());
    upstream.on('close',()=>socket.destroy());
    socket.on('close',()=>{untrack();upstream.destroy();});
  });
  server.on('error',()=>{console.error('Private preview listener unavailable.');process.exit(1);});
  // A single public TCP listener dispatches TLS and legacy HTTP on the same port.
  const legacy=http.createServer({connectionsCheckingInterval:1000},(req,res)=>{
    let c;try{c=config();}catch{return respond(res,503,'Preview configuration unavailable.');}
    if(!permitted(req,port,c))return respond(res,403,'Private device access required.');
    if(!req.url.startsWith('/') || req.url.startsWith('//'))return respond(res,400,'Invalid request.');
    res.writeHead(308,{'location':externalOrigin(c,port)+req.url,'cache-control':'no-store'});
    res.end();
  });
  legacy.on('upgrade',(req,socket)=>socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n'));
  for(const parser of [server,legacy]) {
    parser.requestTimeout=30000;parser.headersTimeout=15000;
    parser.on('clientError',(_error,socket)=>socket.destroy());
    // Parsers never listen themselves; this starts Node's header/request deadline checks.
    parser.emit('listening');
  }
  const listener=net.createServer(socket=>{
    socket.on('error',()=>socket.destroy());
    socket.setTimeout(15000,()=>socket.destroy());
    socket.once('data',chunk=>{
      socket.pause();socket.unshift(chunk);socket.setTimeout(0);
      (chunk[0]===22 ? server : legacy).emit('connection',socket);
      if(chunk[0]!==22)socket.resume();
    });
  });
  listener.on('error',()=>{console.error('Private preview listener unavailable.');process.exit(1);});
  listener.listen(port,initial.bind);
  secureServers.push(server);
  servers.push(listener,server,legacy);
}
process.on('SIGTERM',()=>{for(const s of servers)s.close();setTimeout(()=>process.exit(0),1000).unref();});
