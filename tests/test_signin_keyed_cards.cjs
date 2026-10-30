'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
// A small tree DOM runs the actual card markup and reconciliation, including node moves and selection loss.
let selection;
class Node {
  constructor(tag='div',attrs={}){this.tag=tag;this.attrs=attrs;this.dataset={};this.children=[];this.parentElement=null;this.text='';
    this.className=attrs.class || '';this.classList={toggle(){}};this.options=[];this.style={};
    for(const [key,value] of Object.entries(attrs))if(key.startsWith('data-'))this.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value;}
  get firstElementChild(){return this.children[0] || null;}
  get textContent(){return this.text+this.children.map(c=>c.textContent).join('');}
  set textContent(value){this.text=String(value);this.children=[];}
  get innerHTML(){return this.markup || '';}
  set innerHTML(value){this.markup=value;this.children=parse(value);for(const child of this.children)child.parentElement=this;}
  addEventListener(){} focus(){document.activeElement=this;} removeAttribute(k){delete this.attrs[k];}
  setAttribute(k,v){this.attrs[k]=v;} getAttribute(k){return this.attrs[k] ?? null;}
  contains(node){return node===this || this.children.some(c=>c.contains(node));}
  matches(selector){if(selector.startsWith('.'))return this.className.split(/\s+/).includes(selector.slice(1));
    const attr=selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
    return attr ? Object.hasOwn(this.attrs,attr[1]) && (attr[2]===undefined || this.attrs[attr[1]]===attr[2]) : this.tag===selector;}
  querySelector(selector){for(const child of this.children){if(child.matches(selector))return child;const found=child.querySelector(selector);if(found)return found;}return null;}
  remove(){if(!this.parentElement)return;
    if(this.contains(selection.anchorNode)){selection.anchorNode=selection.focusNode=null;}
    this.parentElement.children=this.parentElement.children.filter(c=>c!==this);this.parentElement=null;}
  insertBefore(node,before){node.remove();const index=before?this.children.indexOf(before):this.children.length;
    assert(index>=0);this.children.splice(index,0,node);node.parentElement=this;return node;}
  // moveBefore preserves selection and other state in a connected node.
  moveBefore(node,before){const parent=node.parentElement;parent.children=parent.children.filter(c=>c!==node);
    const index=before?this.children.indexOf(before):this.children.length;assert(index>=0);this.children.splice(index,0,node);node.parentElement=this;}
  append(node){this.insertBefore(node,null);} after(node){const parent=this.parentElement;parent.insertBefore(node,parent.children[parent.children.indexOf(this)+1] || null);}
  replaceWith(node){const parent=this.parentElement;parent.insertBefore(node,this);this.remove();}
}
function parse(html){const root=new Node(),stack=[root];
  for(const token of html.match(/<[^>]+>|[^<]+/g) || []){
    if(token.startsWith('</')){stack.pop();continue;}
    if(!token.startsWith('<')){stack.at(-1).text+=token;continue;}
    const tag=token.match(/^<([\w-]+)/)?.[1];if(!tag)continue;const attrs={};
    for(const [,key,value] of token.matchAll(/([\w-]+)="([^"]*)"/g))attrs[key]=value;
    const node=new Node(tag,attrs);node.parentElement=stack.at(-1);node.parentElement.children.push(node);
    if(!['input','wbr','br','hr','img','meta','link'].includes(tag) && !token.endsWith('/>'))stack.push(node);
  }return root.children;
}
const elements=new Map();
const document={documentElement:{dataset:{}},body:new Node('body'),activeElement:null,
  getElementById(id){if(!elements.has(id))elements.set(id,new Node());return elements.get(id);},
  createElement(tag){const node=new Node(tag);if(tag==='template')Object.defineProperty(node,'content',{get:()=>node});return node;},
  querySelector:s=>document.getElementById('account-grid').querySelector(s),querySelectorAll:()=>[]};
document.activeElement=document.body;
const c=vm.createContext({document,Date,JSON,Math,CSS:{escape:v=>v},localStorage:{getItem:()=>null},
  window:{getSelection:()=>selection},BillingDates:{describe:()=>({})}});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);
c.renderRouting=()=>{};
const account=(id)=>({id,key:id+'.json',email:id+'@example.test',windows:[],signin:{instance:'boot'},availability:{state:'ready'}});
const a=account('a'),b=account('b'),d=account('d');
b.signin.session={handle:'same-session',state:'pending',user_code:'ABCD-EFGH',expires_at:Date.now()/1000+900,
  verification_url:'https://auth.openai.com/codex/device',expected_email:b.email};
const grid=document.getElementById('account-grid');
for(const modern of [true,false]){
  selection={anchorNode:null,focusNode:null,anchorOffset:0,focusOffset:0,
    setBaseAndExtent(a,ao,f,fo){Object.assign(this,{anchorNode:a,anchorOffset:ao,focusNode:f,focusOffset:fo});}};
  grid.moveBefore=modern?Node.prototype.moveBefore:undefined;grid.innerHTML='';document.activeElement=document.body;
  c.fixture={gateway:{online:true,checked_at:new Date().toISOString(),accounts:[a,b]},read_only:false};
  vm.runInContext('current=fixture;failed=false',c);c.renderAccounts(c.fixture.gateway);
  const original=grid.children[1],panel=original.querySelector('.signin-panel'),code=panel.querySelector('code');
  document.activeElement=panel.querySelector('a');selection.setBaseAndExtent(code,1,code,6);
  for(const accounts of [[d,a,b],[b,d,a],[a,b],[b,a]]){
    c.fixture.gateway.accounts=accounts;c.renderAccounts(c.fixture.gateway);
    assert.deepEqual(grid.children.map(card=>card.dataset.accountKey),accounts.map(a=>a.key));
    const card=grid.children.find(card=>card.dataset.accountKey===b.key);
    assert.equal(card,original,'insertion/reordering must retain the card with the same account key');
    assert.equal(card.querySelector('.signin-panel'),panel);assert.equal(panel.querySelector('code'),code);
    assert.equal(selection.anchorNode,code);assert.equal(selection.anchorOffset,1);assert.equal(selection.focusOffset,6);
    assert.equal(document.activeElement,panel.querySelector('a'));
  }
  // A changed session must replace the old panel even with the same filename.
  b.signin.session.handle='next-session';c.renderAccounts(c.fixture.gateway);
  assert.notEqual(grid.children[0].querySelector('.signin-panel'),panel);
  b.signin.session.handle='same-session';
}
console.log('Sign-in keyed cards: insertion, reorder, removal, session replacement, node identity and code selection passed');
