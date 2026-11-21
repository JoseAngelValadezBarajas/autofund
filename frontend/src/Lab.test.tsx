import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {LabApp,type LabStatus} from './Lab';

const base=():LabStatus=>({
  product:'lab',mode:'LAB_PAPER',live_trading:'NOT_REPRESENTABLE',checked_at:'2026-11-23T15:00:00Z',warnings:[],
  certification:'UNCERTIFIED',credentials:{configured:true,paper_key:true,error:null},
  portfolio:{account:{status:'ACTIVE',equity:'100000',cash:'90000',buying_power:'180000',day_change:'-50',trading_blocked:false},
    clock:{is_open:true,next_open:'o',next_close:'c'},positions:[{symbol:'SPY',qty:'2',market_value:'1000',avg_entry_price:'490',current_price:'500',unrealized_pl:'20'}],
    orders:[{id:'1',symbol:'SPY',side:'buy',status:'filled',notional:'1000',filled_qty:'2',filled_avg_price:'500',submitted_at:''}]},
  capture:{running:false,interval_seconds:30,symbols:['SPY','QQQ'],started_at:null,stopped_at:null,stop_reason:null,polls:0,
    quotes_written:0,in_session_quotes:0,errors:0,last_error:null,last_poll_at:null,files:0,bytes:0,directory:'research/capture',
    note:'Capture runs only while this application is open and stops when it exits.'},
  session:{state:'STOPPED',session_id:null,strategy_id:null,capital_fraction:null,auto_rebalance:false,started_at:null,halt_reason:null,
    last_rebalance:null,last_plan:null,last_execution:null,start_phrase:'START PAPER EXPERIMENT',flatten_phrase:'FLATTEN PAPER',
    limits:{max_capital_fraction:'0.5',max_order_fraction:'0.25',min_order:'10'}},
  research:[{id:'H-A2',decision:'NO_TRADE',selected_lookback:189,holdout_net_sharpe:'0.25',benchmark_net_sharpe:'0.42',timing_control_p:'0.57',trials_counted:6}],
  strategies:[{id:'momentum-126',label:'Trend filter, 126 days',lookback:126,universe:['SPY'],description:'Hold while above.'}]});

let status:LabStatus,calls:{url:string;method:string;body?:any}[];
const plan={plan_id:'abc123',strategy_id:'momentum-126',as_of:'2026-11-20',equity:'100000',capital:'10000',executable:true,blocked:[],
  orders:[{symbol:'SPY',action:'BUY',notional:'2500',current_value:'0',target_value:'2500'}]};

beforeEach(()=>{
  status=base();calls=[];
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:any)=>{
    const method=init?.method??'GET';calls.push({url,method,body:init?.body?JSON.parse(init.body):undefined});
    const json=(b:any,ok=true)=>({ok,json:async()=>b});
    if(url==='/api/v1/control/session')return json({control_token:'tok',start_phrase:'START PAPER EXPERIMENT',flatten_phrase:'FLATTEN PAPER'});
    if(url==='/api/v1/lab/status')return json(status);
    if(url.startsWith('/api/v1/lab/signals/'))return json({rows:[{symbol:'SPY',as_of:'2026-11-20',last_close:'500',trailing_return_pct:'5.00',action:'HOLD LONG'}]});
    if(url.startsWith('/api/v1/lab/journal'))return json({items:[{t:'2026-11-23T15:00:00Z',event:'PLAN'}]});
    if(url==='/api/v1/lab/equity')return json({items:[]});
    if(url==='/api/v1/lab/session/plan')return json(plan);
    if(['/api/v1/lab/session/start','/api/v1/lab/capture/start','/api/v1/lab/session/kill','/api/v1/lab/session/execute'].includes(url))return json({});
    return json({},false)}));
});
afterEach(()=>{cleanup();vi.unstubAllGlobals()});
const posts=()=>calls.filter(c=>c.method==='POST');

test('always says paper only, uncertified and that live trading cannot exist',async()=>{
  render(<LabApp/>);
  expect(await screen.findByText('PAPER MONEY ONLY')).toBeTruthy();
  expect(screen.getAllByText('UNCERTIFIED').length).toBeGreaterThan(0);
  expect(screen.getByText(/NOT REPRESENTABLE/)).toBeTruthy();
  expect(screen.getByText(/UNCERTIFIED EXPERIMENT/)).toBeTruthy();
});

test('shows the paper account, positions and the private research evidence',async()=>{
  render(<LabApp/>);
  expect(await screen.findByText('$100,000.00')).toBeTruthy();
  expect(screen.getAllByText('SPY').length).toBeGreaterThan(0);
  expect(screen.getByText('H-A2')).toBeTruthy();
  expect(screen.getByText('NO_TRADE')).toBeTruthy();
});

test('capture is started by the operator and the in-app limit is stated',async()=>{
  render(<LabApp/>);
  expect(await screen.findByText(/ends when you close the app/)).toBeTruthy();
  fireEvent.click(screen.getByText('Start capture'));
  await waitFor(()=>expect(posts().some(c=>c.url==='/api/v1/lab/capture/start'&&c.body.interval_seconds===30)).toBe(true));
});

test('a session cannot start until the exact phrase is typed',async()=>{
  render(<LabApp/>);
  fireEvent.click(await screen.findByText('Start session…'));
  const authorize=screen.getByText('Authorize') as HTMLButtonElement;
  expect(authorize.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('Authorization phrase'),{target:{value:'start paper experiment'}});
  expect(authorize.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('Authorization phrase'),{target:{value:'START PAPER EXPERIMENT'}});
  expect(authorize.disabled).toBe(false);
  fireEvent.click(authorize);
  await waitFor(()=>expect(posts().find(c=>c.url==='/api/v1/lab/session/start')?.body).toMatchObject(
    {strategy_id:'momentum-126',capital_fraction:'0.1',confirmation:'START PAPER EXPERIMENT',auto_rebalance:false}));
});

test('previews a server-built plan and offers no symbol or size input',async()=>{
  render(<LabApp/>);
  fireEvent.click(await screen.findByText('Preview plan'));
  expect(await screen.findByText(/Plan abc123/)).toBeTruthy();
  expect(screen.getByText('BUY')).toBeTruthy();
  expect(screen.queryByLabelText(/symbol/i)).toBeNull();
  expect(screen.queryByLabelText(/quantity|shares/i)).toBeNull();
});

test('executing needs queuing consent when the market is closed',async()=>{
  status.session.state='ACTIVE';status.session.session_id='paper-1';status.portfolio!.clock.is_open=false;
  render(<LabApp/>);
  fireEvent.click(await screen.findByText('Preview plan'));
  const execute=await screen.findByText('Execute plan (paper)') as HTMLButtonElement;
  expect(execute.disabled).toBe(true);
  fireEvent.click(screen.getByLabelText(/queue these orders/));
  expect(execute.disabled).toBe(false);
  fireEvent.click(execute);
  await waitFor(()=>expect(posts().find(c=>c.url==='/api/v1/lab/session/execute')?.body).toEqual({plan_id:'abc123',queue_if_closed:true}));
});

test('kill is one confirmation away and sends the control token',async()=>{
  render(<LabApp/>);
  fireEvent.click(await screen.findByText('KILL'));
  fireEvent.click(screen.getByText('Kill now'));
  await waitFor(()=>expect(posts().some(c=>c.url==='/api/v1/lab/session/kill')).toBe(true));
  const init=(fetch as any).mock.calls.find((c:any[])=>c[0]==='/api/v1/lab/session/kill')[1];
  expect(init.headers['X-AutoFund-Control-Token']).toBe('tok');
});

test('missing keys are explained and trading controls are disabled',async()=>{
  status.credentials={configured:false,paper_key:false,error:'x'};status.portfolio=null;
  status.warnings=['Alpaca paper keys are not configured.'];
  render(<LabApp/>);
  expect(await screen.findByText(/paper keys are not configured/)).toBeTruthy();
  expect((screen.getByText('Start capture') as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByText('Preview plan') as HTMLButtonElement).disabled).toBe(true);
});
