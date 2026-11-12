'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../scripts/router-preview.mjs'),'utf8');
const allowed=vm.runInNewContext(source.match(/function dashboardRequestAllowed\(req\) \{[\s\S]*?\n\}/)[0]+'\ndashboardRequestAllowed');
for(const route of ['/api/claude-warnings/dismiss','/api/accounts/billing','/api/layout','/api/accounts/add/start','/api/accounts/add/callback','/api/accounts/add/cancel','/api/accounts/routing','/api/accounts/usage/refresh','/api/accounts/disconnect','/api/accounts/api-keys/add']){
  assert(allowed({method:'POST',url:route}));
  assert(allowed({method:'POST',url:route+'?test=1'}));
  assert(!allowed({method:'POST',url:route+'/extra'}));
  assert(!allowed({method:'POST',url:route+'/'}));
  for(const method of ['GET','PATCH','DELETE'])assert(!allowed({method,url:route}));
}
assert(allowed({method:'GET',url:'/api/accounts/add/status?session=synthetic'}));
for(const route of ['/api/requests','/api/requests?provider=claude&limit=50'])assert(allowed({method:'GET',url:route}));
for(const method of ['POST','PATCH','DELETE','HEAD'])assert(!allowed({method,url:'/api/requests'}));
for(const url of ['/api/requests/','/api/requests/extra'])assert(!allowed({method:'GET',url}));
for(const method of ['GET','PATCH','DELETE'])assert(!allowed({method,url:'/api/accounts/disconnect'}));
assert(!allowed({method:'POST',url:'/api/accounts/add/status'}));
for(const method of ['GET','PATCH','DELETE'])assert(!allowed({method,url:'/api/claude-warnings/dismiss'}));
assert(!allowed({method:'POST',url:'/api/claude-warnings/dismiss/'}));
console.log('Router preview account allowlist passed');
