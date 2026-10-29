async function signinBrowserChecks() {
  const results=[];
  const check=(value,name)=>{if(!value)throw new Error(name);results.push(name);};
  const original={current,failed,load,accountRequest,request,execCommand:document.execCommand,
    clipboard:Object.getOwnPropertyDescriptor(navigator,'clipboard')};
  const fixture=structuredClone(current);
  const account=fixture.gateway.accounts[0];
  const pause=milliseconds=>new Promise(resolve=>setTimeout(resolve,milliseconds));
  const session={handle:'synthetic-browser-session',state:'pending',user_code:'ABCD-EFGH',
    verification_url:'https://auth.openai.com/codex/device',expected_email:account.email,
    started_at:Date.now()/1000,expires_at:Date.now()/1000+900};
  try {
    current=fixture;failed=false;
    load=async()=>{};
    request=async()=>fixture;
    signinErrors.clear();routePending.clear();routeErrors.clear();routeConfirm.clear();
    account.auth_state='sign_in_required';account.disabled=false;account.plan_type='pro';
    account.active=true;account.availability={state:'ready'};
    account.signin={instance:'browser-check',session};
    renderAccounts(current.gateway);
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw new Error('blocked');}}});
    document.execCommand=()=>false;
    await copySignin(account.id);
    const oldCode=document.querySelector('[data-signin-code]');
    check(window.getSelection().toString()===session.user_code,'manual-copy selection starts');
    renderAccounts(current.gateway);
    check(oldCode.isConnected && oldCode===document.querySelector('[data-signin-code]'),'poll retains the same code DOM');
    check(window.getSelection().toString()===session.user_code,'manual-copy selection survives polling');
    const selected=document.activeElement;
    selected.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
    check(document.activeElement===selected,'Escape without confirmation preserves focus and DOM');
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{}}});
    await copySignin(account.id);
    check(document.querySelector('[data-signin-copy]').textContent.includes('Copied'),'Copied appears');
    await pause(2100);
    check(document.querySelector('[data-signin-copy]').textContent==='Copy','Copied expires after two seconds');
    session.expires_at=Date.now()/1000+120;renderAccounts(current.gateway);
    check(document.querySelector('.signin-status').textContent.includes('2 min'),'120 second boundary');
    session.expires_at=Date.now()/1000+48;renderAccounts(current.gateway);
    check(document.querySelector('.signin-status').textContent.includes('48 s'),'under two minutes uses seconds');
    session.expires_at=Date.now()/1000-1;renderAccounts(current.gateway);
    check(document.querySelector('.signin-status').textContent.includes('Code expired'),'expiry shown before next overview');
    check(document.querySelector('[data-signin-copy]').disabled,'expired code cannot copy');
    session.expires_at=Date.now()/1000+900;
    accountError(signinErrors,account.id,"Couldn't reach OpenAI.");
    reconcileAccess(current.gateway,null,fastLoadEpoch+1);renderAccounts(current.gateway);
    check(!document.querySelector('.signin-status').textContent.includes("Couldn't reach"),'authoritative session clears start error');
    account.signin={instance:'browser-check',session:null,last_outcome:{kind:'save_failed',at:Date.now()/1000},retry_allowed:true};
    account.auth_state='authenticated';renderAccounts(current.gateway);
    check(document.querySelector('.account-access-detail').textContent.includes("Couldn't save"),'save failure survives healthy claim');
    check(!!document.querySelector('[data-signin-start]'),'backend-authorized retry is offered');
    account.signin={instance:'browser-check',session};
    renderAccounts(current.gateway);
    document.querySelector('[data-signin-open]').focus();
    account.signin={instance:'browser-check',session:null,last_outcome:{kind:'signed_in',at:Date.now()/1000}};
    renderAccounts(current.gateway);
    check(document.activeElement.dataset.cardid===account.id,'paid success focuses its card');
    for(let poll=0;poll<3;poll++) {
      const previousCard=document.activeElement;
      renderAccounts(current.gateway);
      check(document.activeElement!==previousCard && document.activeElement.dataset.cardid===account.id,'paid card focus survives poll '+poll);
    }
    const routingMode=[...document.querySelectorAll('.routing-control select')]
      .find(select=>!select.disabled && select.getClientRects().length);
    check(!!routingMode,'visible routing control is available');
    routingMode.focus();
    check(document.activeElement===routingMode,'focus leaves the account card');
    renderAccounts(current.gateway);
    check(document.activeElement===routingMode,'card does not steal focus after leaving');
    account.signin.last_outcome=null;
    for(const other of fixture.gateway.accounts)if(other!==account)other.disabled=true;
    await saveAccountRouting(account.id,false);
    check(document.activeElement.dataset.routeKeep===account.id,'last-ready confirmation focuses Keep on');
    document.querySelector('[data-route-off]').focus();
    document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
    check(!document.querySelector('.route-confirm'),'Escape dismisses open confirmation');
    check(document.activeElement.dataset.routeAccount===account.id,'Escape restores switch focus');
    for(const blocked of ['readonly','offline','signed_out']) {
      confirmRouting(account);
      current.read_only=blocked==='readonly';current.gateway.online=blocked!=='offline';account.auth_state=blocked==='signed_out'?'sign_in_required':'authenticated';
      renderAccounts(current.gateway);
      check(!document.querySelector('.route-confirm'),'confirmation dismissed when '+blocked);
    }
    current.read_only=false;current.gateway.online=true;account.plan_type='pro';account.auth_state='authenticated';
    accountError(routeErrors,account.id,'Change not confirmed.');
    account.disabled=true;reconcileAccess(current.gateway,null,fastLoadEpoch+1);renderAccounts(current.gateway);
    check(!routeErrors.has(account.id),'authoritative routing state clears lost-response error');
    let refreshed=0;
    load=async()=>{refreshed++;};
    document.dispatchEvent(new Event('visibilitychange'));
    check(refreshed===1,'visible-tab event refreshes overview');
    account.auth_state='sign_in_required';account.disabled=false;account.signin={instance:'browser-check'};
    renderAccounts(current.gateway);
    check(!document.querySelector('#codex-notices,#claude-notices,#account-notices'),'Overview has no notice containers');
    let posts=0,complete;
    accountRequest=()=>{posts++;return new Promise(resolve=>{complete=resolve;});};
    const button=document.querySelector('[data-signin-start]');
    button.click();button.click();
    check(posts===1,'card wiring suppresses duplicate starts');
    complete({instance:'browser-check',session});await pause(0);
    account.signin={instance:'browser-check'};
    current.read_only=true;renderAccounts(current.gateway);
    const blockedButton=document.querySelector('[data-signin-start]');
    check(blockedButton.disabled && blockedButton.title==='Sign in from your computer dashboard','card read-only reason');
    if(innerWidth<=760)check(blockedButton.getBoundingClientRect().height>=38,'card mobile touch target');
    check(document.documentElement.scrollWidth<=innerWidth,'no horizontal overflow');
    return {passed:results.length,results};
  } finally {
    current=original.current;failed=original.failed;load=original.load;accountRequest=original.accountRequest;request=original.request;
    document.execCommand=original.execCommand;
    if(original.clipboard)Object.defineProperty(navigator,'clipboard',original.clipboard);else delete navigator.clipboard;
    signinErrors.clear();routePending.clear();routeErrors.clear();routeConfirm.clear();copiedCodes.clear();
    render();
  }
}
window.signinBrowserResult=signinBrowserChecks();

window.signinVisualGallery=async function(group) {
  const states=group==='signin' ? ['required','starting','pending','copied','finishing','paid','free','free-signed',
    'wrong-account','wrong-workspace','expired','declined','provider-error','save-failed','identity-unavailable'] :
    ['cancelled','start-error','cancel-error','connection-lost','expired-code','routing-on','routing-off',
     'confirmation','route-error','read-only','unreachable'];
  const baseline=structuredClone(current), gallery=document.createElement('div'), results=[];
  gallery.style.cssText=`display:grid;grid-template-columns:repeat(${innerWidth>=1180?4:1},minmax(0,1fr));gap:20px;padding:16px;align-items:start`;
  for(const state of states) {
    current=structuredClone(baseline);failed=false;
    signinBusy.clear();signinErrors.clear();routePending.clear();routeErrors.clear();routeConfirm.clear();copiedCodes.clear();
    const account=current.gateway.accounts[0];
    Object.assign(account,{auth_state:'sign_in_required',disabled:false,active:true,plan_type:'pro',plan:'Pro (10x)',
      availability:{state:'ready'},access_error:null,signin:{session:null,last_outcome:null}});
    const session={handle:'visual-'+state,user_code:'ABCD-EFGH',state:'pending',expected_email:account.email,
      verification_url:'https://auth.openai.com/codex/device',expires_at:Date.now()/1000+900,started_at:Date.now()/1000};
    current.read_only=false;current.gateway.online=true;
    if(['pending','copied','finishing','cancel-error','connection-lost','expired-code'].includes(state))account.signin.session=session;
    if(state==='starting')signinBusy.add(account.id);
    if(state==='copied')copiedCodes.set(session.handle,Date.now()+2000);
    if(state==='finishing')session.state='finishing';
    if(state==='expired-code')session.expires_at=Date.now()/1000-1;
    if(state==='connection-lost')failed=true;
    if(state==='start-error')accountError(signinErrors,account.id,"Couldn't reach OpenAI.");
    if(state==='cancel-error')accountError(signinErrors,account.id,"Couldn't cancel. Try again.");
    if(['free','free-signed'].includes(state))Object.assign(account,{disabled:false,active:true,plan_type:'free',plan:'Free',auth_state:'authenticated'});
    if(state==='free-signed')account.signin.last_outcome={kind:'signed_in',at:Date.now()/1000};
    const outcomes={'paid':'signed_in','wrong-account':'wrong_account','wrong-workspace':'wrong_workspace',
      'expired':'expired','declined':'declined','provider-error':'provider_error','save-failed':'save_failed',
      'identity-unavailable':'identity_unavailable'};
    if(outcomes[state])account.signin.last_outcome={kind:outcomes[state],approved_email:'other@example.com',at:Date.now()/1000};
    if(state==='save-failed')account.signin.retry_allowed=true;
    if(['paid','routing-on','routing-off','confirmation','route-error','read-only','unreachable'].includes(state)){
      account.auth_state='authenticated';account.quota_error=null;
    }
    if(state==='routing-off'){account.disabled=true;account.active=false;}
    if(state==='confirmation'){
      for(const other of current.gateway.accounts)if(other!==account)other.disabled=true;
      confirmRouting(account);
    }
    if(state==='route-error')accountError(routeErrors,account.id,'Change not confirmed. Try again.');
    if(state==='read-only')current.read_only=true;
    if(state==='unreachable')current.gateway.online=false;
    renderAccounts(current.gateway);
    const card=document.querySelector(`[data-cardid="${CSS.escape(account.id)}"]`);
    const buttons=[...card.querySelectorAll('[data-signin-start],[data-signin-copy],.signin-open,[data-signin-cancel],.route-confirm button')];
    const result={state,overflow:document.documentElement.scrollWidth>innerWidth,
      touchTargets:innerWidth>760 || buttons.every(button=>button.hidden || button.getBoundingClientRect().height>=38)};
    if(result.overflow || !result.touchTargets)throw new Error('Visual layout failed: '+state);
    results.push(result);
    const section=document.createElement('section'), caption=document.createElement('h3');
    caption.textContent=state;caption.style.cssText='font-size:12px;margin:0 0 8px;color:var(--muted)';
    section.append(caption,card.cloneNode(true));gallery.append(section);
  }
  const sprite=document.querySelector('symbol').parentNode.cloneNode(true);
  for(let timer=1;timer<10000;timer++)clearInterval(timer);
  document.body.replaceChildren(sprite,gallery);scrollTo(0,0);
  await document.fonts.ready;
  return {group,results,height:document.documentElement.scrollHeight};
};
