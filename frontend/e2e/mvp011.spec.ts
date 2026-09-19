import {expect,test} from '@playwright/test';

/** MVP 0.1.1 postmortem, learning and scanner pages, over a seeded fixture app. */
const BASE='http://127.0.0.1:8030';

test.describe.configure({mode:'serial'});

/** Volatile values are masked so the baselines stay deterministic. */
const maskVolatile=(page:any)=>[
  page.getByText(/^\d+s ago$/),page.getByText(/^\d+s$/),
  page.getByText(/Closes in .*s/),page.getByText(/observation time .* ago/),
  // Per-run identifiers and real timestamps.
  page.getByText(/^mvp-\d{8}T\d{6}-[a-f0-9]{8}$/),
  page.getByText(/af-mvp-pending/),
  page.locator('td',{hasText:/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}/}),
  // Retained historical market timestamp on the stopped-session telemetry view.
  page.locator('b',{hasText:/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}/}),
  page.locator('dd',{hasText:/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}/}),
  page.locator('dt,dd',{hasText:/Fingerprint|fingerprint/}),
  // 64-hex fingerprints and their truncated renderings.
  page.locator('text=/^[a-f0-9]{16,}…?$/'),
  // The candle series and depth values legitimately vary with live market data.
  page.locator('figure',{has:page.getByLabel('BTC/MXN candlestick chart')}),
  page.locator('[data-volatile="true"]'),
];

const shot=async(page:any,name:string,testInfo:any)=>{
  if(testInfo.project.name==='desktop')await expect(page).toHaveScreenshot(name,
    {animations:'disabled',fullPage:true,mask:maskVolatile(page)});
};

test('postmortem, learning and scanner surfaces are visible without Production traffic',async({page},testInfo)=>{
  const external:string[]=[],mutations:string[]=[],errors:string[]=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('console',m=>{if(m.type()==='error')errors.push(m.text())});
  page.on('request',r=>{
    if(!r.url().startsWith(BASE)&&!r.url().startsWith('data:'))external.push(r.url());
    if(['POST','PUT','PATCH','DELETE'].includes(r.method()))mutations.push(r.method()+' '+new URL(r.url()).pathname);
  });
  await page.goto(BASE+'/');

  // Completed session: elapsed is frozen and the stop reason is backend-authoritative.
  await page.getByRole('button',{name:'Sessions'}).click();
  await expect(page.getByText('MAX_SESSION_DURATION_REACHED')).toBeVisible();
  await expect(page.getByText('Actual duration')).toBeVisible();
  await expect(page.getByText('Configured max duration')).toBeVisible();
  await expect(page.getByText('Stop reason')).toBeVisible();
  await expect(page.getByText('Ended')).toBeVisible();
  await expect(page.getByText('IN PROGRESS')).toHaveCount(0);
  await shot(page,'mvp-0_1_1-sessions-postmortem-desktop.png',testInfo);

  // Telemetry: deliberately stopped runtime is INACTIVE, never STALE or INVALID.
  await page.getByRole('button',{name:'Telemetry'}).click();
  await expect(page.getByText('ONLINE',{exact:true})).toBeVisible();
  await expect(page.getByText('CONNECTED',{exact:true})).toBeVisible();
  await expect(page.getByText('INACTIVE').first()).toBeVisible();
  await expect(page.getByText('Last market quality')).toBeVisible();
  await expect(page.getByText('STALE',{exact:true})).toHaveCount(0);
  await expect(page.getByText('INVALID',{exact:true})).toHaveCount(0);
  await shot(page,'mvp-0_1_1-telemetry-inactive-desktop.png',testInfo);

  // Pipeline: a completed NO_SIGNAL path marks downstream stages NOT_APPLICABLE.
  await expect(page.getByLabel('Autonomous pipeline')).toBeVisible();
  await expect(page.getByText('NOT_APPLICABLE').first()).toBeVisible();
  await expect(page.getByText('UNKNOWN',{exact:true})).toHaveCount(0);

  // Learning: champion evidence with no fabricated challengers.
  await page.getByRole('button',{name:'Learning'}).click();
  await expect(page.getByRole('heading',{name:'Current Champion'})).toBeVisible();
  await expect(page.getByText('mean-reversion-safe')).toBeVisible();
  await expect(page.getByText('Eligible evaluations').first()).toBeVisible();
  await expect(page.getByText('ENTRY_CONDITION_NOT_MET').first()).toBeVisible();
  await expect(page.getByText('INSUFFICIENT_EVIDENCE').first()).toBeVisible();
  await expect(page.getByText(/No challenger exists/)).toBeVisible();
  await expect(page.getByRole('heading',{name:'Market opportunity research'})).toBeVisible();
  await shot(page,'mvp-0_1_1-learning-desktop.png',testInfo);

  // Scanner: research candidates with several distinct statuses.
  await page.getByRole('button',{name:'Scanner'}).click();
  await expect(page.getByText('READ-ONLY RESEARCH')).toBeVisible();
  await expect(page.getByText('RESEARCH CANDIDATE').first()).toBeVisible();
  await expect(page.getByText('INELIGIBLE_CAP')).toBeVisible();
  await expect(page.getByText('btc_mxn').first()).toBeVisible();
  await expect(page.getByText('Score components')).toBeVisible();
  await expect(page.getByText(/Scanner ranks research candidates, not financial actions/)).toBeVisible();
  for(const banned of ['BUY NOW','SELL NOW','RECOMMENDED COIN','BEST COIN TO BUY'])await expect(page.getByText(banned)).toHaveCount(0);
  for(const name of ['BUY','SELL','PROMOTE','SET LIVE MARKET'])await expect(page.getByRole('button',{name})).toHaveCount(0);
  await shot(page,'mvp-0_1_1-market-scanner-desktop.png',testInfo);

  // Research functionality is read-only and never mutates anything.
  expect(mutations).toEqual([]);
  expect(external).toEqual([]);
  expect(errors).toEqual([]);
});

test('scanner page never exposes an automatic market promotion control',async({page})=>{
  await page.goto(BASE+'/');
  await page.getByRole('button',{name:'Scanner'}).click();
  await expect(page.getByText('Automatic market rotation').first()).toBeVisible();
  await expect(page.getByText('NOT PRESENT').first()).toBeVisible();
  await expect(page.getByText('FUTURE MANUAL MILESTONE')).toBeVisible();
  const controls=await page.locator('input, select, textarea').count();
  expect(controls).toBe(0);
});
