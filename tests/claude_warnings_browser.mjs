// Run through heavy with PLAYWRIGHT_MODULE pointing to an installed Playwright package.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const [url,shots]=process.argv.slice(2);
if(!url || !shots)throw Error('Usage: node tests/claude_warnings_browser.mjs <fixture URL> <screenshot directory>');
await fs.mkdir(shots,{recursive:true});
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM?{executablePath:process.env.PLAYWRIGHT_CHROMIUM}:{})});
const errors=[],results=[];
try{
  const context=await browser.newContext({timezoneId:'America/Santiago',locale:'en-US',reducedMotion:'reduce'});
  const page=await context.newPage();
  page.on('pageerror',error=>errors.push(String(error)));
  const select=async state=>{
    const response=await context.request.post(url+'/fixture',{headers:{'X-Router-Request':'1'},data:{state}});
    assert.equal(response.status(),200);
  };
  const states=['none','one-low','both-low','one-out','all-out','all-out-soon','all-out-codex','stacked','routing-off','reset-due','signed-out','duplicate-low','out-and-low','subscription-payment','subscription-ended','subscription-active'];
  for(const theme of ['dark','light'])for(const width of [1440,768,390])for(const state of states){
    await select(state);
    await page.setViewportSize({width,height:width===390?844:930});
    await page.addInitScript(theme=>localStorage.setItem('router-theme',theme),theme);
    await page.goto(`${url}/?state=${state}`);
    await page.waitForFunction(()=>current && document.querySelectorAll('.claude-card').length===2);
    await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);
    await page.evaluate(()=>document.fonts.ready);
    const banner=page.locator('.allowance-banner');
    if(state==='none' || state.startsWith('subscription-'))assert.equal(await banner.count(),0);
    else{
      assert.equal(await banner.count(),1);
      const text=await banner.innerText();
      if(['one-low','both-low','one-out','duplicate-low','out-and-low'].includes(state)){
        assert.match(text,/Claude running low/);
        assert.equal(await banner.locator('.allowance-rows>li').count(),state==='both-low'?3:state==='out-and-low'?2:1);
        if(['one-out','out-and-low'].includes(state))assert.match(text,/Used up/);
        if(width===390){
          const close=await banner.locator('#claude-low-dismiss').boundingBox(),head=await banner.locator('.allowance-head').boundingBox();
          assert.ok(close.width>=32 && close.height>=32,'dismiss keeps a touch-sized target');
          assert.ok(Math.abs((close.y+close.height/2)-(head.y+head.height/2))<=1,'dismiss shares the title line');
        }
      }else{
        assert.match(text,/All Claude accounts are out/);
        assert.equal(await banner.locator('button').count(),0);
        assert.match(text,state==='all-out-codex'?/Codex has no ready account either/:/Switch to OpenAI/);
        if(state==='all-out-soon')assert.match(text,/Weekly 2% left/);
        if(state==='routing-off')assert.match(text,/Routing off[\s\S]*29% left/);
        if(state==='reset-due')assert.match(text,/Reset due · checking/);
        if(state==='signed-out')assert.match(text,/Signed out/);
      }
      const box=await banner.boundingBox();assert.ok(box.x>=0 && box.x+box.width<=width+1,`${state} banner overflow`);
      // Each account row stays on one line at every width: long emails truncate, and narrow screens move the exact time to the countdown's tooltip.
      for(const li of await banner.locator('.allowance-rows>li').all()){
        const tops=await li.evaluate(el=>[...el.children].map(c=>Math.round(c.getBoundingClientRect().top)));
        assert.ok(Math.max(...tops)-Math.min(...tops)<=1,`${state} row wraps at ${width}`);
      }
      if(width<1440 && await banner.locator('.allowance-clock').count()){
        assert.ok((await banner.locator('.allowance-clock').first().boundingBox()).width<=1,`${state} clock hides at ${width}`);
        assert.match(await banner.locator('.allowance-when [title]').first().getAttribute('title'),/ in .+ · .+[AP]M$/);
      }
    }
    const token=async(name,scope='body')=>page.evaluate(({name,scope})=>{const el=document.createElement('span');el.style.color=`var(${name})`;document.querySelector(scope).append(el);const value=getComputedStyle(el).color;el.remove();return value;},{name,scope});
    if(state==='one-low'){
      const card=page.locator('.claude-card').first();
      assert.match(await card.locator('.state-text').textContent(),/Near limit/);
      // The details wait in the state popover, which opens on hover.
      assert.match(await card.locator('.state-detail').textContent(),/new sessions go to/);
      await card.locator('.state-text').hover();
      assert.match(await page.locator('#state-tip').innerText(),/Near limit[\s\S]*new sessions go to/);
      await page.mouse.move(0,0);
      assert.equal(await page.locator('#state-tip').isHidden(),true);
      assert.match(await card.locator('.limit-row .low').first().innerText(),/8% remaining/);
      assert.ok(await card.locator('.pace').count()>0,'near-limit cue must preserve current pace');
    }
    if(state==='both-low'){
      assert.match(await page.locator('.claude-card').nth(1).locator('.quota-used .low').innerText(),/94% used/);
      for(const card of await page.locator('.claude-card').all())assert.match(await card.locator('.state-detail').textContent(),/All Claude accounts are near their limits/);
    }
    if(['one-out','all-out'].includes(state)){
      const outCards=page.locator('.claude-card.is-out');
      assert.equal(await outCards.count(),state==='one-out'?1:2);
      for(const card of await outCards.all()){
        assert.equal(await card.locator('.quota-value').evaluate(el=>getComputedStyle(el).color),await token('--faint','.claude-card'));
        assert.equal(await card.locator('.quota-track>span').first().evaluate(el=>getComputedStyle(el).backgroundColor),await token('--faint','.claude-card'));
        assert.match(await card.locator('.state-text').textContent(),/Out · back in .+ · .+[AP]M/);
      }
    }
    if(state==='signed-out'){
      assert.equal(await banner.locator('.out-back').count(),0);
      assert.equal(await banner.locator('.allowance-when').allTextContents().then(v=>v.every(t=>t==='Sign in on your computer')),true);
      for(const card of await page.locator('.claude-card').all())assert.match(await card.locator('.state-text').textContent(),/Sign in required/);
    }
    if(state==='reset-due'){
      const due=banner.locator('.out-back');
      assert.equal(await due.innerText(),'Reset due · checking');
      assert.equal(await due.locator('time,strong').count(),0);
      assert.equal(await due.locator('span').evaluate(el=>getComputedStyle(el).fontSize),'12px');
      assert.doesNotMatch(await banner.locator('.allowance-when').first().innerText(),/[AP]M/);
    }
    if(state.startsWith('subscription-')){
      // The subscription lives in the plan badge on the email's line: no row of its own, the problem in its tone.
      const card=page.locator('.claude-card').first(), badge=card.locator('.account-top a.plan-badge');
      assert.equal(await page.locator('.claude-card .subscription').count(),0);
      assert.equal(await badge.getAttribute('href'),'https://claude.ai/settings/billing');
      assert.equal(await badge.locator('svg').count(),1);
      assert.match(await badge.innerText(),{'subscription-payment':/^Payment due$/,'subscription-ended':/^Subscription ended$/,'subscription-active':/^Active$/}[state]);
      assert.equal(await badge.evaluate(el=>getComputedStyle(el).color),await token(state==='subscription-payment'?'--amber':state==='subscription-ended'?'--danger':'--accent','.claude-card'));
      const top=await card.locator('.account-top').boundingBox(),box=await badge.boundingBox();
      assert.ok(box.y>=top.y-1 && box.y+box.height<=top.y+top.height+1 && box.x+box.width<=top.x+top.width+1,`${state} badge stays on the name line`);
    }
    if(await banner.locator('.out-switch').count()){
      assert.equal(await banner.locator('.out-switch').evaluate(el=>getComputedStyle(el).color),await token('--muted'));
      assert.equal(await banner.locator('.out-switch').evaluate(el=>getComputedStyle(el).fontWeight),'400');
    }
    for(const time of await banner.locator('.allowance-when time').all()){
      assert.equal(await time.evaluate(el=>getComputedStyle(el).color),await token('--muted'));
      assert.match(await time.evaluate(el=>el.previousSibling.textContent),/ · $/);
    }
    if(state==='stacked'){
      assert.equal(await page.locator('#notice').isVisible(),true);
      const notice=await page.locator('#notice').boundingBox(),out=await banner.boundingBox();
      assert.ok(notice.y+notice.height<out.y);
      assert.match(await page.locator('.claude-card').first().locator('.state-text').textContent(),/Status stale/);
    }
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
    assert.equal(overflow,false,`${state} page overflow at ${width}`);
    const name=`${state}-${theme}-${width}.png`;
    await page.screenshot({path:path.join(shots,name),fullPage:true,animations:'disabled'});
    if(['both-low','all-out','reset-due','stacked'].includes(state))await page.screenshot({path:path.join(shots,name.replace('.png','-viewport.png')),animations:'disabled'});
    results.push({state,theme,width,screenshot:name});
  }
  await select('all-out');await page.goto(url+'/');await page.waitForSelector('.allowance-banner.out');
  for(const tab of ['usage','system']){
    await page.locator(`a[href="#${tab}"]`).first().click();
    assert.equal(await page.locator('.allowance-banner.out').isVisible(),true);
    await page.screenshot({path:path.join(shots,`all-out-light-390-${tab}.png`),fullPage:true});
  }
  // Failed writes remain visible and expose an actionable error.
  await select('duplicate-low');await page.goto(url+'/');await page.waitForSelector('#claude-low-dismiss');
  await page.route('**/api/claude-warnings/dismiss',route=>route.fulfill({status:503,contentType:'application/json',body:'{"error":"unavailable"}'}));
  await page.locator('#claude-low-dismiss').click();
  await page.waitForSelector('.dismiss-error');
  assert.match(await page.locator('.dismiss-error').innerText(),/Warning not dismissed. Try again/);
  await page.screenshot({path:path.join(shots,'dismiss-failure-light-390.png'),fullPage:true});
  await page.unroute('**/api/claude-warnings/dismiss');
  await page.locator('#claude-low-dismiss').click();await page.waitForSelector('.allowance-banner',{state:'detached'});
  // A separate browser context sees the desktop's persisted dismissal.
  const phone=await browser.newContext({viewport:{width:390,height:844}}),second=await phone.newPage();
  await second.goto(url+'/');await second.waitForFunction(()=>current && document.querySelectorAll('.claude-card').length===2);
  assert.equal(await second.locator('.allowance-banner').count(),0);
  assert.equal(await second.evaluate(()=>current.gateway.claude_warnings.low.length),1);
  assert.equal(await second.evaluate(()=>current.gateway.claude_warnings.low[0].dismissed),true);
  await phone.close();
  assert.deepEqual(errors,[]);
  await fs.writeFile(path.join(shots,'results.json'),JSON.stringify({results,errors,crossDeviceDismissal:true,bannerPages:['overview','usage','system']},null,2));
  console.log(`Claude warning browser checks: ${results.length} state/theme/viewport captures; all pages, persistent dismissal, no overflow or JS errors`);
}finally{await browser.close();}
