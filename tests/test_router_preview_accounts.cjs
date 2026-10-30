'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../scripts/router-preview.mjs'),'utf8');
const allowed=vm.runInNewContext(source.match(/function dashboardRequestAllowed\(req\) \{[\s\S]*?\n\}/)[0]+'\ndashboardRequestAllowed');
for(const route of ['/api/accounts/add/start','/api/accounts/add/callback','/api/accounts/add/cancel','/api/accounts/routing']){
  assert(allowed({method:'POST',url:route}));
  assert(allowed({method:'POST',url:route+'?test=1'}));
  assert(!allowed({method:'POST',url:route+'/extra'}));
}
assert(allowed({method:'GET',url:'/api/accounts/add/status?session=synthetic'}));
for(const method of ['GET','POST','PATCH','DELETE'])assert(!allowed({method,url:'/api/accounts/disconnect'}));
assert(!allowed({method:'POST',url:'/api/accounts/add/status'}));
console.log('Router preview account allowlist passed');
