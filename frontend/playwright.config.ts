import {defineConfig,devices}from'@playwright/test';
import {join} from 'node:path';

/**
 * The interpreter path is resolved rather than hardcoded.
 *
 * Every `webServer.command` below used to begin with `.venv\Scripts\autofund`, the Windows layout.
 * On Linux that path does not exist, so Playwright could not start the fixture servers and the
 * whole end-to-end job failed with exit code 127 before running a single test. The suite had only
 * ever run on the machine it was written on.
 *
 * `path.join` is used instead of a separator literal so the result is correct on both platforms.
 * Writing the Windows form with a POSIX separator produces a path like `.venv\Scripts/autofund`,
 * which Windows refuses to execute — the mistake made by the first attempt at this fix.
 */
const venvBin=join('.venv',process.platform==='win32'?'Scripts':'bin');
const autofund=join(venvBin,'autofund');
const python=join(venvBin,'python');

export default defineConfig({
  workers:1,
  testDir:'./e2e',
  use:{baseURL:'http://127.0.0.1:8010',screenshot:'only-on-failure',trace:'retain-on-failure'},
  projects:[
    {name:'desktop',use:{viewport:{width:1440,height:900}}},
    {name:'narrow',testIgnore:['**/mvp.spec.ts','**/mvp011.spec.ts'],use:{...devices['Desktop Chrome'],viewport:{width:390,height:844}}}
  ],
  webServer:[
    {command:`${autofund} dashboard --demo-micro-live --port 8002`,cwd:'..',url:'http://127.0.0.1:8002',reuseExistingServer:false},
    {command:`${autofund} dashboard --demo --port 8010`,cwd:'..',url:'http://127.0.0.1:8010',reuseExistingServer:false},
    {command:`${autofund} dashboard --demo-live --port 8001`,cwd:'..',url:'http://127.0.0.1:8001',reuseExistingServer:false},
    {command:`${autofund} app --demo --no-open-browser --port 8020 --artifacts artifacts/playwright-mvp`,cwd:'..',url:'http://127.0.0.1:8020/api/v1/mvp',reuseExistingServer:false},
    {command:`${python} scripts/seed_mvp_fixture.py --port 8030 --artifacts artifacts/playwright-mvp-fixture`,cwd:'..',url:'http://127.0.0.1:8030/api/v1/mvp',reuseExistingServer:false}
  ]
});
