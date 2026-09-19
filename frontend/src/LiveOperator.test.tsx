import {cleanup,fireEvent,render,screen,within} from '@testing-library/react';
import {afterEach,expect,test} from 'vitest';
import {LiveActivityPage,LiveMarketPage,LiveOperatorView,LiveTelemetryPage,CandleChart,type MvpObservability} from './LiveOperator';

afterEach(cleanup);

type Overrides=Partial<MvpObservability['runtime']>&{observability?:Partial<MvpObservability['observability']>};
type Model=Partial<Omit<MvpObservability,'runtime'|'observability'>> & Overrides;

const baseRuntime:(over?:Overrides)=>MvpObservability['runtime']=over=>({
  status:'RUNNING',heartbeat:'LIVE',quality:'VALID',mode:'MVP-AUTONOMOUS',elapsed_seconds:42,
  last_event_at:'2026-09-18T10:00:05Z',last_market_event_at:'2026-09-18T10:00:05Z',
  last_closed_candle_at:'2026-09-18T09:59:00Z',current_candle:null,market_snapshot:null,events:[],
  ...over});

const baseEvents:MvpObservability['runtime']['events']=[
  {event_id:1,event_type:'MARKET_CONNECTED',timestamp:'2026-09-18T10:00:00Z',summary:'market ready',component:'market',level:'INFO'},
  {event_id:2,event_type:'CANDLE_CLOSED',timestamp:'2026-09-18T10:00:01Z',summary:'closed candle',component:'market',level:'INFO'},
  {event_id:3,event_type:'STRATEGY_EVALUATED',timestamp:'2026-09-18T10:00:02Z',summary:'champion evaluated',component:'strategy',level:'INFO'},
  {event_id:4,event_type:'NO_SIGNAL',timestamp:'2026-09-18T10:00:03Z',summary:'Champion chose no trade',component:'strategy',level:'INFO'},
];

const waiting:MvpObservability['pipeline']=['MARKET','CANDLE','STRATEGY','SIGNAL','CAPITAL','RISK','FINAL_MARKET_CHECK','ORDER','FILL','RECONCILIATION','LEDGER']
  .map(stage=>({stage,status:'WAITING',at:null,event:null,detail:null}));

function model(over:Model={}):MvpObservability{
  const {observability,...rest}=over;
  return {
    runtime:baseRuntime(rest as Overrides),
    observability:{status:'HEALTHY',degraded:false,scope:'BACKEND_TELEMETRY_RUNTIME_HEALTH',
      trading_effect:'NONE',browser_tab_closure_effect:'NONE',heartbeat_age_seconds:0,
      publication_age_seconds:0,...observability},
    market_state:'PROCESSING_MARKET_DATA',
    pipeline:waiting,
    candles:[],
    strategy:{last_evaluation_at:null,evaluations:0,signals:0,no_signal:0,last_decision:null},
    session:{elapsed_seconds:42,remaining_seconds:3558,max_duration_seconds:3600,max_orders_per_session:10,
      max_session_loss_mxn:'10'},
    metrics:{},
    ...rest,
  } as MvpObservability;
}

const renderOperator=(m:MvpObservability)=>render(<LiveOperatorView model={m} now={Date.now()}
  sessionId="mvp-1" equity="50" orders={0} fills={0} champion="mean-reversion" regime="NORMAL"/>);

test('RUNNING with market events shows price, candle and live heartbeat',()=>{
  const m=model({market_snapshot:{last_price_mxn:'999900',best_bid_mxn:'999900',best_ask_mxn:'1000000',
    spread_mxn:'100',spread_bps:'1',orderbook_at:'2026-09-18T10:00:05Z',sequence:77,request_latency_ms:12},
    current_candle:{status:'OPEN',interval_start:'2026-09-18T10:00:00Z',interval_end:'2026-09-18T10:00:59Z',
      open:'999900',high:'1000100',low:'999800',last:'1000050',volume:'0',trade_count:3}});
  renderOperator(m);
  expect(screen.getAllByText('999900').length).toBeGreaterThan(0);
  expect(screen.getAllByText('1000000').length).toBeGreaterThan(0);
  expect(screen.getByText('77')).toBeTruthy();
  expect(screen.getByText('100 MXN / 1 bps')).toBeTruthy();
  expect(screen.getByLabelText('BTC/MXN candlestick chart')).toBeTruthy();
  expect(screen.getByText('PROCESSING MARKET DATA')).toBeTruthy();
  // Market request RTT lives on the live Market page, not the Overview summary.
  render(<LiveMarketPage model={m} now={Date.now()}/>);
  expect(screen.getAllByText('12 ms').length).toBeGreaterThan(0);
});

test('RUNNING waiting for first market event is distinguishable from processing',()=>{
  renderOperator(model({market_state:'WAITING_FOR_FIRST_MARKET_EVENT',last_market_event_at:null,
    market_snapshot:null,current_candle:null}));
  expect(screen.getByText('WAITING FOR FIRST MARKET EVENT')).toBeTruthy();
  expect(screen.getAllByText(/WAITING FOR DATA/).length).toBeGreaterThan(0);
});

test('RUNNING with stale market data reports stale without inventing values',()=>{
  renderOperator(model({market_state:'MARKET_DATA_STALE',heartbeat:'STALE',quality:'DEGRADED'}));
  expect(screen.getByText('MARKET DATA STALE')).toBeTruthy();
  expect(screen.queryByText('LIVE')).toBeNull();
});

test('RUNNING with disconnected market data reports disconnected',()=>{
  renderOperator(model({market_state:'MARKET_DATA_DISCONNECTED',heartbeat:'DISCONNECTED',status:'DISCONNECTED'}));
  expect(screen.getAllByText('MARKET DATA DISCONNECTED').length).toBeGreaterThan(0);
});

test('RUNNING no-signal loop shows decision, reason and pipeline',()=>{
  const m=model({events:baseEvents,
    strategy:{last_evaluation_at:'2026-09-18T10:00:02Z',evaluations:1,signals:0,no_signal:1,
      last_decision:{at:'2026-09-18T10:00:03Z',decision:'NO_SIGNAL',signal:null,reason:'Champion chose no trade',correlation_id:null}},
    pipeline:waiting.map(s=>s.stage==='MARKET'?{...s,status:'PASS',event:'MARKET_CONNECTED'}
      :s.stage==='CANDLE'?{...s,status:'PASS',event:'CANDLE_CLOSED'}
      :s.stage==='STRATEGY'?{...s,status:'PASS',event:'STRATEGY_EVALUATED'}
      :s.stage==='SIGNAL'?{...s,status:'NONE',event:'NO_SIGNAL'}:s)});
  renderOperator(m);
  expect(screen.getAllByText('NO_SIGNAL').length).toBeGreaterThan(0);
  expect(screen.getAllByText('Champion chose no trade').length).toBeGreaterThan(0);
  const pipeline=screen.getByLabelText('Autonomous pipeline');
  expect(within(pipeline).getByText('Market')).toBeTruthy();
  expect(within(pipeline).getByText('Ledger')).toBeTruthy();
  expect(within(pipeline).getAllByText('WAITING').length).toBeGreaterThan(0);
});

test('RUNNING signal pipeline shows order, fill, reconciliation and ledger results',()=>{
  const chain=['CANDLE','STRATEGY','SIGNAL','CAPITAL','RISK','FINAL_MARKET_CHECK','ORDER','FILL','RECONCILIATION','LEDGER'];
  const m=model({events:[...baseEvents,{event_id:5,event_type:'SIGNAL_GENERATED',timestamp:'2026-09-18T10:00:04Z',
    summary:'BUY',component:'strategy',level:'INFO',correlation_id:'corr-1'},
    {event_id:6,event_type:'FILL',timestamp:'2026-09-18T10:00:05Z',summary:'fill',component:'accounting',level:'INFO',correlation_id:'corr-1'}],
    pipeline:waiting.map(s=>chain.includes(s.stage)?{...s,status:'PASS',event:`${s.stage}_OK`}:s),
    strategy:{last_evaluation_at:'2026-09-18T10:00:02Z',evaluations:1,signals:1,no_signal:0,
      last_decision:{at:'2026-09-18T10:00:04Z',decision:'SIGNAL',signal:'BUY',reason:'Signal generated from a closed candle',correlation_id:'corr-1'}}});
  renderOperator(m);
  expect(screen.getByText('SIGNAL (BUY)')).toBeTruthy();
  const pipeline=screen.getByLabelText('Autonomous pipeline');
  for(const name of ['Order','Fill','Reconciliation','Ledger'])expect(within(pipeline).getByText(name)).toBeTruthy();
  expect(within(pipeline).getByText('Reconciliation')).toBeTruthy();
  expect(screen.getAllByText('corr-1').length).toBeGreaterThan(0);
});

test('RUNNING observability degraded is surfaced and never claims trading effect',()=>{
  renderOperator(model({observability:{status:'OBSERVABILITY_DEGRADED',degraded:true,heartbeat_age_seconds:40,publication_age_seconds:40}}));
  expect(screen.getByText('OBSERVABILITY DEGRADED')).toBeTruthy();
  expect(screen.getByText(/operator view is stale while the backend continues/)).toBeTruthy();
  expect(screen.getByText(/Trading effect: NONE/)).toBeTruthy();
});

test('market page renders chart, open candle and closed candles',()=>{
  const m=model({market_snapshot:{last_price_mxn:'999900',best_bid_mxn:'999900',best_ask_mxn:'1000000',
    spread_mxn:'100',spread_bps:'1',orderbook_at:'2026-09-18T10:00:05Z',sequence:77,request_latency_ms:12},
    current_candle:{status:'OPEN',interval_start:'2026-09-18T10:00:00Z',interval_end:'2026-09-18T10:00:59Z',
      open:'999900',high:'1000100',low:'999800',last:'1000050',volume:'0',trade_count:3},
    candles:[{status:'CLOSED',interval_start:'2026-09-18T09:59:00Z',interval_end:'2026-09-18T09:59:59Z',
      open:'999000',high:'999950',low:'998900',last:'999900',close:'999900',volume:'0',trade_count:5}]});
  render(<LiveMarketPage model={m} now={Date.now()}/>);
  expect(screen.getByLabelText('BTC/MXN candlestick chart')).toBeTruthy();
  expect(screen.getByText('77')).toBeTruthy();
  expect(screen.getAllByText('12 ms').length).toBeGreaterThan(0);
  expect(screen.getByText('2026-09-18T09:59:00Z')).toBeTruthy();
});

test('activity page filters by component, level and event and toggles order',()=>{
  render(<LiveActivityPage events={baseEvents} now={Date.now()}/>);
  const rows=()=>screen.getAllByRole('row').slice(1).map(r=>within(r).getAllByRole('cell')[3].textContent);
  expect(rows()[0]).toBe('NO_SIGNAL');
  fireEvent.click(screen.getByLabelText('Newest first'));
  expect(rows()[0]).toBe('MARKET_CONNECTED');
  fireEvent.change(screen.getByLabelText('Filter by component'),{target:{value:'strategy'}});
  expect(rows().length).toBe(2);
  fireEvent.change(screen.getByLabelText('Filter by event'),{target:{value:'NO_SIGNAL'}});
  expect(rows()).toEqual(['NO_SIGNAL']);
});

test('telemetry page shows runtime metrics, latency and counts',()=>{
  const m=model({metrics:{market_events:12,market_unavailable:1,order_intents:2,fills:2,signals:2,
    risk_reject:1,halts:0,reconciliations:2,ledger_updates:2,closed_candles:4,no_signal:3,capital_reject:0,
    final_market_reject:0,durations_ms:{market_request_rtt:{count:3,last_ms:11.5,max_ms:20.25},
      strategy_evaluation:{count:2,last_ms:0.4,max_ms:0.9}}}});
  render(<LiveTelemetryPage model={m} now={Date.now()}/>);
  expect(screen.getAllByText('HEALTHY').length).toBeGreaterThan(0);
  expect(screen.getByText(/11.5 ms \(max 20.25 ms, n=3\)/)).toBeTruthy();
  expect(screen.getByText(/0.4 ms \(max 0.9 ms, n=2\)/)).toBeTruthy();
  expect(screen.getByText('12')).toBeTruthy();
  expect(screen.getAllByText(/UNKNOWN/).length).toBeGreaterThan(0);
});

test('candle chart shows waiting for data instead of fabricating candles',()=>{
  render(<CandleChart candles={[]} open={null} now={Date.now()}/>);
  expect(screen.getByText('WAITING FOR DATA')).toBeTruthy();
});

test('open candle is rendered as observational and never as a closed input',()=>{
  const{container}=render(<CandleChart candles={[]} open={{status:'OPEN',interval_start:'2026-09-18T10:00:00Z',
    interval_end:'2026-09-18T10:00:59Z',open:'1',high:'2',low:'1',last:'2',volume:'0',trade_count:1}} now={Date.now()}/>);
  expect(container.textContent).toContain('Open candle is OBSERVATIONAL ONLY and never a strategy input');
  expect(container.querySelector('[data-candle="OPEN"]')).toBeTruthy();
  expect(container.querySelector('[data-candle="CLOSED"]')).toBeNull();
});
