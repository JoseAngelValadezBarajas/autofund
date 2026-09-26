import {expect,test} from '@playwright/test';

/**
 * MVP 0.3.0 Research Control Center, over the real seeded fixture app.
 *
 * The end-to-end test exists for the property that no unit test can fully establish:
 * that the separation of engineering state, research evidence, production authorization
 * and current action survives a real HTTP round trip against real registry data. A unit
 * test can prove the component renders four values; only this can prove the backend
 * published four independent ones and that the browser did not fold them together.
 *
 * It also asserts the two invariants that matter most for a product holding real money:
 * that no page in the Control Center can place an order, and that the browser makes no
 * mutation request of any kind while browsing the research surface.
 */
const BASE='http://127.0.0.1:8030';

test.describe.configure({mode:'serial'});

/** Attach read-only guards that fail the test on any external call or any mutation. */
const guard=(page:any)=>{
  const external:string[]=[],mutations:string[]=[],errors:string[]=[];
  page.on('pageerror',(e:Error)=>errors.push(e.message));
  page.on('console',(m:any)=>{if(m.type()==='error')errors.push(m.text())});
  page.on('request',(r:any)=>{
    if(!r.url().startsWith(BASE)&&!r.url().startsWith('data:'))external.push(r.url());
    if(['POST','PUT','PATCH','DELETE'].includes(r.method()))
      mutations.push(r.method()+' '+new URL(r.url()).pathname);
  });
  return {external,mutations,errors};
};

test('the control center separates the four truths and stays read-only',async({page},testInfo)=>{
  const {external,mutations,errors}=guard(page);
  await page.goto(BASE+'/');
  await page.getByRole('button',{name:'Control Center'}).click();

  await expect(page.getByRole('heading',{name:'Research control center',level:2})).toBeVisible();

  // Four independent facts, four separate renderings.
  for(const [dimension,value] of [['ENGINEERING','HEALTHY'],['RESEARCH','ACCUMULATING'],
    ['PRODUCTION','DISABLED'],['ACTION','NO_TRADE']]){
    const node=page.locator(`[data-dimension="${dimension}"]`);
    await expect(node).toBeVisible();
    await expect(node).toHaveAttribute('data-value',value);
  }
  await expect(page.locator('[data-statuses-independent="true"]')).toBeVisible();

  // A healthy system must not read as profitable evidence.
  await expect(page.locator('[data-says-nothing-about-profitability="true"]')).toBeVisible();
  // Positive research evidence must not read as authorization.
  await expect(page.locator('[data-session-authorized="false"]')).toBeVisible();
  // Wallet and inventory are shown separately.
  await expect(page.locator('[data-wallet-is-not-inventory="true"]')).toBeVisible();

  // No manual trade control and no input of any kind on the research surface.
  for(const banned of ['BUY NOW','SELL NOW','PROFITABLE','GUARANTEED','RISK FREE'])
    await expect(page.getByText(banned)).toHaveCount(0);
  await expect(page.locator('input, select, textarea')).toHaveCount(0);

  if(testInfo.project.name==='desktop')
    await expect(page).toHaveScreenshot('mvp-0_3_0-control-center-desktop.png',
      {animations:'disabled',fullPage:true,
       mask:[page.getByText(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/),
             page.locator('text=/^[a-f0-9]{16}/')]});

  expect(mutations).toEqual([]);
  expect(external).toEqual([]);
  expect(errors).toEqual([]);
});

test('the registries show prediction, economics and provenance apart',async({page},testInfo)=>{
  const {external,mutations,errors}=guard(page);
  await page.goto(BASE+'/');

  // Alpha registry: a predictive signal is not a tradeable source.
  await page.getByRole('button',{name:'Alpha Registry'}).click();
  await expect(page.getByRole('heading',{name:'Alpha registry',level:2})).toBeVisible();
  const signal=page.locator('[data-alpha="VALIDATED_INFORMATION_SIGNAL_V1"]');
  await expect(signal).toBeVisible();
  await expect(signal).toHaveAttribute('data-prediction','PREDICTIVE');
  await expect(signal).toHaveAttribute('data-economic','NOT_ECONOMIC');
  await expect(signal).toHaveAttribute('data-tradeable','false');

  // Strategy registry: engineering PASS with economics FAIL on one row.
  await page.getByRole('button',{name:'Strategy Registry'}).click();
  await expect(page.getByRole('heading',{name:'Strategy registry',level:2})).toBeVisible();
  const strategy=page.locator('[data-profile="mean-reversion-safe-v1"]').first();
  await expect(strategy).toHaveAttribute('data-engineering','PASS');
  await expect(strategy).toHaveAttribute('data-economic','FAIL');
  await expect(strategy).toHaveAttribute('data-production-eligible','false');

  // Evidence explorer: provenance distinguishes a measurement from a fixture.
  await page.getByRole('button',{name:'Evidence'}).click();
  await expect(page.getByRole('heading',{name:'Evidence explorer',level:2})).toBeVisible();
  // Scoped to `[data-evidence]`, the row marker. The provenance banner carries `data-provenance`
  // as well, so selecting on that alone also matched the banner and produced a null reading.
  const rows=page.locator('[data-evidence]');
  await expect(rows.first()).toBeVisible();
  expect(await rows.count()).toBeGreaterThan(0);
  // Every row must declare its provenance and whether it is a real observation. A row missing the
  // second would be a null, which is not a verdict either way.
  for(const value of await rows.evaluateAll(
    (nodes:Element[])=>nodes.map(n=>n.getAttribute('data-real-observation'))))
    expect(['true','false']).toContain(value);

  if(testInfo.project.name==='desktop')
    await expect(page).toHaveScreenshot('mvp-0_3_0-evidence-explorer-desktop.png',
      {animations:'disabled',fullPage:true,
       mask:[page.locator('text=/^[a-f0-9]{16}/'),page.locator('.cc-fp')]});

  expect(mutations).toEqual([]);
  expect(external).toEqual([]);
  expect(errors).toEqual([]);
});

test('a healthy collector with insufficient coverage is not read as a market conclusion',
  async({page})=>{
  const {mutations,errors}=guard(page);
  await page.goto(BASE+'/');
  await page.getByRole('button',{name:'Campaigns'}).click();
  await expect(page.getByRole('heading',{name:'Campaigns',level:2})).toBeVisible();

  // Selected by the property under test - a collector that ran correctly AND whose coverage was
  // evaluated as insufficient - rather than by position or by id. Positional selection made this
  // depend on registry ordering, and id selection would hardcode which campaign happens to be in
  // the fixture. Selecting the combination asserts exactly the claim: health and conclusion are
  // independent, and a healthy process can still be unable to support a conclusion.
  const healthyButInsufficient=
    page.locator('[data-campaign][data-process-health="HEALTHY"][data-coverage-sufficient="false"]');
  await expect(healthyButInsufficient.first()).toBeVisible();
  // An unevaluated coverage verdict must be distinguishable from a negative one.
  await expect(page.locator('[data-coverage-sufficient="UNKNOWN"]').first()).toBeVisible();
  // And a collector with no data at all reports health UNKNOWN rather than a confident HEALTHY.
  await expect(page.locator('[data-campaign][data-process-health="UNKNOWN"]').first()).toBeVisible();

  // The page must say why the conclusion is limited rather than implying a market verdict.
  await expect(page.getByText(/a rare event could not have been observed/).first()).toBeVisible();

  expect(mutations).toEqual([]);
  expect(errors).toEqual([]);
});

test('every ineligible market lays out a blocking reason',async({page})=>{
  const {mutations,errors}=guard(page);
  await page.goto(BASE+'/');
  await page.getByRole('button',{name:'Eligibility'}).click();
  await expect(page.getByRole('heading',{name:'Production eligibility',level:2})).toBeVisible();

  const rows=page.locator('[data-market]');
  await expect(rows.first()).toBeVisible();
  const count=await rows.count();
  expect(count).toBeGreaterThan(0);
  const reasons=await rows.evaluateAll(
    (nodes:Element[])=>nodes.map(n=>n.getAttribute('data-blocking-reason')));
  // "Not selected" must never be ambiguous: every row states a cause.
  for(const reason of reasons) expect(reason).toBeTruthy();
  const eligible=await rows.evaluateAll(
    (nodes:Element[])=>nodes.map(n=>n.getAttribute('data-eligible')));
  // Nothing in this project is production eligible, so no row may claim otherwise.
  for(const value of eligible) expect(value).toBe('false');

  expect(mutations).toEqual([]);
  expect(errors).toEqual([]);
});

test('the research surface cannot be made to open a position',async({page})=>{
  const {mutations,errors}=guard(page);
  await page.goto(BASE+'/');
  // Visit every Control Center page, including the registries and the artifact index.
  for(const name of ['Control Center','Alpha Registry','Strategy Registry','Experiments',
    'Evidence','Campaigns','Timeline','Eligibility','Artifacts']){
    await page.getByRole('button',{name}).click();
    await expect(page.getByRole('heading',{level:2}).first()).toBeVisible();
  }
  // No order ticket, no promotion control, and no mutation request anywhere in the walk.
  for(const banned of ['BUY','SELL','PROMOTE','ACTIVATE','ENABLE','FORCE TRADE','WITHDRAW',
    'TRANSFER','ORDER TICKET'])
    await expect(page.getByRole('button',{name:banned})).toHaveCount(0);
  await expect(page.locator('input, select, textarea')).toHaveCount(0);
  expect(mutations).toEqual([]);
  expect(errors).toEqual([]);
});
