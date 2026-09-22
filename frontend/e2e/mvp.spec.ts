import {expect,test} from '@playwright/test';
test.describe.configure({mode:'serial'});

/** Volatile, time-derived values are masked so the baseline is deterministic. */
const maskVolatile=(page:any)=>[page.getByText(/^\d+s ago$/),page.getByText(/^\d+s$/),
  page.getByText(/Closes in .*s/),page.getByText(/observation time .* ago/),
  // Session identifiers are generated per run.
  page.getByText(/^mvp-\d{8}T\d{6}-[a-f0-9]{8}$/),
  // Activity/telemetry tables carry real UTC timestamps that shift every run.
  page.locator('td',{hasText:/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}/}),
  page.locator('dd',{hasText:/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}/}),
  // The candle series legitimately changes when a UTC minute rolls over mid-run.
  // Candle correctness (open vs closed, never a strategy input) is asserted
  // deterministically in the unit tests instead.
  page.locator('figure',{has:page.getByLabel('BTC/MXN candlestick chart')}),
  page.locator('[data-volatile="true"]')];

/** Screenshot only on the desktop project; the app itself is identical. */
const shot=async(page:any,name:string,testInfo:any)=>{
  if(testInfo.project.name==='desktop')await expect(page).toHaveScreenshot(name,
    {animations:'disabled',fullPage:true,mask:maskVolatile(page)});
};

test('MVP autonomous BUY SELL and graceful STOP',async({page},testInfo)=>{
  const errors:string[]=[],external:string[]=[],mutations:string[]=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('console',m=>{if(m.type()==='error')errors.push(m.text())});
  page.on('request',r=>{if(!r.url().startsWith('http://127.0.0.1:8020'))external.push(r.url());
    if(['POST','PUT','PATCH','DELETE'].includes(r.method()))mutations.push(r.method()+' '+new URL(r.url()).pathname)});
  await page.goto('http://127.0.0.1:8020/');
  await expect(page.getByText('STOPPED',{exact:true})).toBeVisible();
  await expect(page.getByText('Production preflight',{exact:true})).toBeVisible();
  await expect(page.getByText(/Production preflight NOT APPLICABLE: DEMO_MODE_NOT_PRODUCTION/)).toBeVisible();
  for(const name of ['BUY','SELL','PLACE ORDER','SKIP PREFLIGHT'])await expect(page.getByRole('button',{name})).toHaveCount(0);
  await shot(page,'mvp-stopped-desktop.png',testInfo);
  await page.getByRole('button',{name:'START AUTOFUND'}).click();
  await page.getByLabel('Strong confirmation').fill('START AUTOFUND REAL 50');
  await page.getByRole('button',{name:'AUTHORIZE REAL SESSION'}).click();
  await expect(page.getByText('AUTO EXECUTION ON',{exact:true})).toBeVisible();
  // The live operator view replaces the summary line: orders/fills are now cards.
  await expect(page.getByText('Orders used / max')).toBeVisible({timeout:8000});
  await expect(page.getByText('2 / 10')).toBeVisible();
  await expect(page.getByText('Session loss limit')).toBeVisible();

  // Overview live operator view.
  await expect(page.getByLabel('Autonomous pipeline')).toBeVisible();
  await expect(page.getByLabel('BTC/MXN candlestick chart')).toBeVisible();
  await expect(page.getByText('Session id',{exact:true})).toBeVisible();
  await expect(page.getByText(/Elapsed/)).toBeVisible();
  await shot(page,'mvp-running-live-observability-desktop.png',testInfo);

  // Market page.
  await page.getByRole('button',{name:'Market',exact:true}).click();
  await expect(page.getByLabel('BTC/MXN candlestick chart')).toBeVisible();
  await expect(page.getByText('Current open candle',{exact:true})).toBeVisible();
  await expect(page.getByText('Recent closed candles',{exact:true})).toBeVisible();
  await shot(page,'mvp-running-market-desktop.png',testInfo);

  // Activity page with filters.
  await page.getByRole('button',{name:'Activity',exact:true}).click();
  await expect(page.getByLabel('Filter by component')).toBeVisible();
  await expect(page.getByLabel('Filter by level')).toBeVisible();
  await expect(page.getByLabel('Filter by event')).toBeVisible();
  await expect(page.getByText(/\d+ checkpoints/)).toBeVisible();
  await expect(page.getByRole('cell',{name:'POSITION_CLOSED'})).toBeVisible();
  await expect(page.getByRole('columnheader',{name:'Correlation'})).toBeVisible();
  await shot(page,'mvp-running-activity-desktop.png',testInfo);

  // Wallet is account evidence only and remains visibly separate from the
  // AutoFund-owned portfolio. It never exposes exchange mutation controls.
  await page.getByRole('button',{name:'Wallet',exact:true}).click();
  await expect(page.getByText('BITSO WALLET — READ ONLY',{exact:true})).toBeVisible();
  await expect(page.getByText('AUTOFUND OWNED',{exact:true})).toBeVisible();
  await expect(page.getByText(/may contain funds that do not belong/)).toBeVisible();
  for(const name of ['BUY','SELL','SEND','WITHDRAW','TRANSFER'])
    await expect(page.getByRole('button',{name})).toHaveCount(0);
  await shot(page,'mvp-running-wallet-desktop.png',testInfo);

  // Telemetry page.
  await page.getByRole('button',{name:'Telemetry',exact:true}).click();
  await expect(page.getByText('Observability',{exact:true})).toBeVisible();
  await expect(page.getByText('Market request RTT',{exact:true})).toBeVisible();
  await expect(page.getByText('Strategy evaluation',{exact:true})).toBeVisible();
  await shot(page,'mvp-running-telemetry-desktop.png',testInfo);

  await page.getByRole('button',{name:'Positions'}).click();
  await expect(page.getByText('No AutoFund-owned position')).toBeVisible();
  await page.getByRole('button',{name:'STOP SESSION'}).click();
  await expect(page.getByText('STOPPED',{exact:true})).toBeVisible();
  expect(mutations.every(x=>/POST \/api\/v1\/control\/(start|stop)$/.test(x))).toBeTruthy();
  expect(external).toEqual([]);
  expect(errors).toEqual([]);
});

test('MVP emergency kill halts without liquidation control',async({page},testInfo)=>{
  await page.goto('http://127.0.0.1:8020/');
  await page.getByRole('button',{name:'START AUTOFUND'}).click();
  await page.getByLabel('Strong confirmation').fill('START AUTOFUND REAL 50');
  await page.getByRole('button',{name:'AUTHORIZE REAL SESSION'}).click();
  await page.getByRole('button',{name:'EMERGENCY KILL'}).click();
  await page.getByRole('button',{name:'KILL NOW'}).click();
  await expect(page.getByText('HALTED',{exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'START AUTOFUND'})).toHaveCount(0);
  for(const name of ['BUY','SELL','PLACE ORDER','CANCEL ORDER','WITHDRAW','TRANSFER'])await expect(page.getByRole('button',{name})).toHaveCount(0);
  await shot(page,'mvp-halted-desktop.png',testInfo);
});
