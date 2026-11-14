// Usage: node check-multi-clip.cjs PAGE_HTML TEST_MP4 [BROWSER_ENV_MODULE]
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { chromium } = process.argv[4] ? require(process.argv[4]).playwright() : require('playwright');
(async () => {
 const browser = await chromium.launch({headless:true});
 try {
  const page = await browser.newPage({viewport:{width:1120,height:900}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  const requests=[];
  await page.route('http://clip.test/**',async route=>{
   const path=new URL(route.request().url()).pathname;
   if(path==='/media')return route.fulfill({contentType:'video/mp4',body:fs.readFileSync(process.argv[3])});
   if(path==='/clip'){requests.push(route.request().postDataJSON());return route.fulfill({status:202,json:{id:'test'}})}
   if(path==='/result/test')return route.fulfill({json:{done:true}});
   return route.fulfill({contentType:'text/html',body:fs.readFileSync(process.argv[2])});
  });
  await page.goto('http://clip.test/');
  await page.waitForFunction(()=>document.querySelector('video').readyState>=2);
  const set=async(a,b)=>{await page.locator('#start').fill(String(a));await page.locator('#end').fill(String(b))};
  await set(0.2,0.8);await page.locator('#addRange').click();
  await set(1,1.7);await page.locator('#addRange').click();
  assert.equal(await page.locator('#ranges li').count(),2);
  await page.locator('#ranges li').nth(1).getByRole('button',{name:'↑',exact:true}).click();
  await set(1.1,1.8);await page.locator('#updateRange').click();
  await page.locator('#range').click();
  await page.waitForFunction(()=>{const v=document.querySelector('video');return v.paused&&v.currentTime>=1.8});
  await set(2,1);await page.locator('#addRange').click();
  assert.equal(await page.locator('#ranges li').count(),2);
  await page.locator('#export').click();
  await page.waitForFunction(()=>!document.querySelector('#export').disabled);
  assert.deepEqual(requests[0],{ranges:[[1.1,1.8],[0.2,0.8]]});
  await page.locator('#ranges li').first().getByRole('button',{name:'削除',exact:true}).click();
  await page.locator('#ranges li').first().getByRole('button',{name:'削除',exact:true}).click();
  await set(0.3,0.9);await page.locator('#export').click();
  await page.waitForFunction(()=>!document.querySelector('#export').disabled);
  assert.deepEqual(requests[1],{ranges:[[0.3,0.9]]});
  assert.deepEqual(errors,[]);
  console.log('PASS: add/update/reorder/delete, real range playback, ordered export payload, single-range fallback');
 } finally {await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
