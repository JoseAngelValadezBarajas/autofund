import {expect,test} from '@playwright/test';

test('micro-live read-only lifecycle and visible money warning',async({page})=>{
  const errors:string[]=[],writes:string[]=[],external:string[]=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('console',message=>{if(message.type()==='error')errors.push(message.text())});
  page.on('request',request=>{
    if(request.url().includes('/api/')&&request.method()!=='GET')writes.push(request.method());
    if(!request.url().startsWith('http://127.0.0.1:8002'))external.push(request.url());
  });
  await page.goto('http://127.0.0.1:8002/');
  await expect(page.getByText('DEMO DATA',{exact:true})).toBeVisible();
  await expect(page.getByText('AWAITING OPERATOR',{exact:true})).toBeVisible();
  await expect(page.getByText('AUTO EXECUTION DISABLED',{exact:true})).toBeVisible();
  await expect(page.getByText('REAL MONEY',{exact:true}).last()).toBeVisible();
  await expect(page.getByText('WRITE CAPABILITY BLOCKED',{exact:true})).toBeVisible();
  await expect(page).toHaveScreenshot(`micro-live-${test.info().project.name}.png`,{animations:'disabled',fullPage:true});
  for(const state of ['SUBMITTED','PARTIALLY FILLED','FILLED','RECONCILED'])await expect(page.getByText(state,{exact:true})).toBeVisible({timeout:15000});
  await expect(page.getByText('0.000005 BTC',{exact:true})).toBeVisible();
  for(const name of ['Market','Activity','Ledger','Sessions','System','Overview']){
    await page.getByRole('button',{name,exact:true}).click();
    await expect(page.getByRole('heading',{name,exact:true})).toBeVisible();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
    expect(await page.getByRole('button',{name:/^(BUY|SELL|PLACE ORDER|CANCEL ORDER|ENABLE LIVE TRADING|WITHDRAW|TRANSFER|CONFIRM)$/i}).count()).toBe(0);
  }
  expect(writes).toEqual([]);expect(errors).toEqual([]);expect(external).toEqual([]);
});
