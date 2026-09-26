import {defineConfig,devices}from'@playwright/test';
import {existsSync} from 'node:fs';
import {join} from 'node:path';

/**
 * How AutoFund is invoked for the end-to-end fixture servers.
 *
 * Four problems had to be solved here, and each fix exposed the next. They are recorded because the
 * final form looks arbitrary otherwise, and each step cost a CI run.
 *
 * **1. The path was Windows-only.** Commands began with `.venv\Scripts\autofund`, the Windows
 * layout, so on Linux Playwright could not start its servers and the job failed with exit code 127
 * before running a test. The directory now comes from `process.platform` and is joined with
 * `path.join`, because a separator literal produces `.venv\Scripts/autofund`, which Windows will not
 * execute.
 *
 * **2. There may be no virtual environment.** CI installs with `actions/setup-python` and
 * `pip install -e`, putting `autofund` on `PATH` and never creating a `.venv`. So the local
 * environment is preferred and the bare command is the fallback.
 *
 * **3. The lookup was relative to the wrong directory.** This file is in `frontend/`, so a relative
 * `.venv` tested `frontend/.venv`, which does not exist even locally. `import.meta.dirname` anchors
 * it to this file.
 *
 * **4. Windows names the script `autofund.exe`.** `existsSync` needs the real filename: a shell
 * resolves `autofund` through `PATHEXT`, but a filesystem check does not, so testing for the
 * extensionless name reported "no virtual environment" on Windows.
 *
 * The command is left unquoted so the shell can apply `PATHEXT` on Windows, and the bare fallback is
 * unchanged so CI resolves `autofund` from `PATH`.
 */
const repoRoot=join(import.meta.dirname,'..');
const venvBin=join(repoRoot,'.venv',process.platform==='win32'?'Scripts':'bin');
// A virtualenv console script is `autofund.exe` on Windows and extensionless elsewhere.
const autofundCandidates=['autofund','autofund.exe','autofund.cmd','autofund.bat']
  .map(name=>join(venvBin,name));
const pythonCandidates=['python','python.exe','python3'].map(name=>join(venvBin,name));

const pick=(candidates:string[],fallback:string)=>
  candidates.find(candidate=>existsSync(candidate))??fallback;

const autofund=pick(autofundCandidates,'autofund');
const python=pick(pythonCandidates,'python');

export default defineConfig({
  workers:1,
  testDir:'./e2e',
  // Visual-regression baselines are captured on Windows and are OS-suffixed by Playwright
  // (`...-desktop-win32.png`). Font rasterisation and subpixel antialiasing differ enough between
  // operating systems that a Linux comparison against a Windows bitmap is not a meaningful test -
  // it reports "differs" for every image and hides the behavioural assertions behind noise.
  //
  // `ignoreSnapshots` on non-Windows hosts makes that explicit and honest: the pixel comparisons are
  // skipped rather than silently failing, while every behavioural assertion in the same files still
  // runs. Regenerating baselines per OS would mean committing a second set that nobody has verified
  // visually, which is worse than saying plainly where the baselines come from.
  //
  // `.github/workflows/ci.yml` states the same thing; `tests/public/test_public_boundary.py` asserts
  // both so the claim and the behaviour cannot drift apart.
  ignoreSnapshots: process.platform !== 'win32',
  use:{baseURL:'http://127.0.0.1:8010',screenshot:'only-on-failure',trace:'retain-on-failure'},
  projects:[
    {name:'desktop',use:{viewport:{width:1440,height:900}}},
    {name:'narrow',testIgnore:['**/mvp.spec.ts','**/mvp011.spec.ts'],use:{...devices['Desktop Chrome'],viewport:{width:390,height:844}}}
  ],
  webServer:[
    {command:`${autofund} dashboard --demo-micro-live --port 8002`,cwd:repoRoot,url:'http://127.0.0.1:8002',reuseExistingServer:false},
    {command:`${autofund} dashboard --demo --port 8010`,cwd:repoRoot,url:'http://127.0.0.1:8010',reuseExistingServer:false},
    {command:`${autofund} dashboard --demo-live --port 8001`,cwd:repoRoot,url:'http://127.0.0.1:8001',reuseExistingServer:false},
    {command:`${autofund} app --demo --no-open-browser --port 8020 --artifacts artifacts/playwright-mvp`,cwd:repoRoot,url:'http://127.0.0.1:8020/api/v1/mvp',reuseExistingServer:false},
    {command:`${python} scripts/seed_mvp_fixture.py --port 8030 --artifacts artifacts/playwright-mvp-fixture`,cwd:repoRoot,url:'http://127.0.0.1:8030/api/v1/mvp',reuseExistingServer:false}
  ]
});
