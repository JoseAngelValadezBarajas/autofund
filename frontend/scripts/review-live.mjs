import {chromium,expect} from '@playwright/test';
import {mkdir,writeFile} from 'node:fs/promises';
const directory=process.env.AUTOFUND_REVIEW_DIRECTORY??'../artifacts/f46/browser';
const browser=await chromium.launch();
const page=await browser.newPage({viewport:{width:1440,height:900}});
const errors=[],writes=[],observed=new Set();
page.on('pageerror',e=>errors.push(e.message));
page.on('console',m=>{if(m.type()==='error')errors.push(m.text())});
page.on('request',r=>{if(r.url().includes('/api/')&&r.method()!=='GET')writes.push(r.method())});
await mkdir(directory,{recursive:true});
await page.goto('http://127.0.0.1:8000/');
await page.getByRole('button',{name:'Market',exact:true}).click();
let snapshots=[];
for(let i=0;i<150;i++){
  const r=await page.request.get('http://127.0.0.1:8000/api/v1/runtime');
  const view=await r.json();
  snapshots.push(view);
  if(!observed.has(view.status)){observed.add(view.status);await expect(page.getByLabel('Session runtime')).toContainText(view.status);await page.screenshot({path:`${directory}/real-${view.status.toLowerCase()}.png`,fullPage:true})}
  if(view.current_candle&&!observed.has('OPEN')){await expect(page.getByLabel('Open candle')).toBeVisible();observed.add('OPEN');await page.screenshot({path:`${directory}/real-open-candle.png`,fullPage:true})}
  if(view.last_closed_candle_at&&!observed.has('CLOSED')){observed.add('CLOSED');await page.getByRole('button',{name:'Activity',exact:true}).click();await page.screenshot({path:`${directory}/real-activity.png`,fullPage:true});await page.getByRole('button',{name:'Market',exact:true}).click()}
  if(view.status==='STOPPED'||view.status==='HALTED')break;
  await page.waitForTimeout(2000);
}
await writeFile(`${directory}/review.json`,JSON.stringify({observed:[...observed],errors,writes,snapshots},null,2));
console.log(JSON.stringify({observed:[...observed],errors,writes}));
await browser.close();
