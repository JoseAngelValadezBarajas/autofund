/**
 * Capture the public-safe screenshots referenced by the README.
 *
 * Run while a demo server is listening on 8040:
 *
 *   node scripts/capture-docs.mjs
 *
 * The screenshots must only ever contain demo data. The script asserts that before it writes each
 * file, because a committed screenshot showing a real balance is a disclosure that cannot be
 * rotated: once it is in Git history it can be rewritten but not unpublished.
 *
 * Plain Node ESM rather than TypeScript, matching scripts/review-live.mjs, so it runs with no
 * additional tooling installed.
 */
import {chromium} from '@playwright/test';
import {mkdir} from 'node:fs/promises';

const BASE = process.env.CAPTURE_BASE ?? 'http://127.0.0.1:8040';
const OUT = process.env.CAPTURE_OUT ?? '../docs/screenshots';

// The real account values that were removed during public-release preparation. Finding one on
// screen means a real artifact was sold to the demo, which is a defect rather than a detail.
const REAL_VALUES = ['0.00000727', '10.91486406', '1501356.8170', 'e4noqPIc3YmZVm0E'];

async function assertSyntheticOnly(page, label) {
  const content = await page.content();
  if (!content.includes('DEMO MODE')) throw new Error(`${label}: no DEMO MODE banner`);
  if (!content.includes('SYNTHETIC DATA')) throw new Error(`${label}: not labelled synthetic`);
  for (const leaked of REAL_VALUES) {
    if (content.includes(leaked)) throw new Error(`${label}: real value ${leaked} is on screen`);
  }
  console.log(`  ${label}: laballed synthetic, no real values present`);
}

const SHOTS = [
  ['Control Center', 'control-center-overview'],
  ['Alpha Registry', 'alpha-registry'],
  ['Strategy Registry', 'strategy-registry'],
  ['Evidence', 'evidence-explorer'],
  ['Campaigns', 'campaigns'],
  ['Eligibility', 'production-eligibility'],
];

const browser = await chromium.launch();
const page = await browser.newPage({viewport: {width: 1440, height: 900}});
await mkdir(OUT, {recursive: true});

let failed = 0;
for (const [nav, filename] of SHOTS) {
  await page.goto(BASE + '/');
  await page.getByRole('button', {name: nav}).click();
  await page.waitForTimeout(1000);
  try {
    await assertSyntheticOnly(page, nav);
  } catch (error) {
    console.error(`  REFUSED: ${error.message}`);
    failed += 1;
    continue;
  }
  await page.screenshot({
    path: `${OUT}/${filename}.png`,
    fullPage: true,
    animations: 'disabled',
    // Mask whatever changes between runs, so the images stay stable and carry no build metadata.
    mask: [
      page.locator('.cc-meta dd'),
      page.locator('text=/^\\d{4}-\\d{2}-\\d{2}T/'),
      page.locator('.cc-fp'),
    ],
  });
  console.log(`  wrote ${filename}.png`);
}

await browser.close();
if (failed) {
  console.error(`CAPTURE FAILED: ${failed} page(s) could not be verified as synthetic`);
  process.exit(1);
}
console.log('screenshots captured');
