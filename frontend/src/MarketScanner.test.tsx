import {cleanup,render,screen} from '@testing-library/react';
import {afterEach,expect,test} from 'vitest';
import {LearningPage,MarketScannerPage,type LearningView,type ScannerEvidence} from './MarketScanner';

afterEach(cleanup);

const candidate=(over:any={})=>({
  book:'btc_mxn',rank:1,status:'ELIGIBLE',reason:'Tradable within the current 50/25/11 MXN envelope',
  score:'79.92',score_version:'autofund.market-opportunity.v1',
  components:{movement_score:'0.75',volatility_score:'0.75',liquidity_score:'0.4',depth_score:'0.2',
    spread_cost_score:'0.99',fee_cost_score:'0.64',slippage_score:'0.99',data_quality_score:'1'},
  movement_bps:'200',volatility_bps:'200',high_low_range_bps:'200',best_bid_mxn:'1000000',
  best_ask_mxn:'1000010',spread_bps:'0.1',depth_mxn:'200',volume_mxn:'500',minimum_order_mxn:'10',
  maker_fee:'0.0065',taker_fee:'0.0078',estimated_round_trip_friction_mxn:'0.1716',
  estimated_round_trip_friction_bps:'156',data_quality:'VALID',cap_executable:true,
  strategy_compatibility:'CERTIFIED_FOR_MARKET',lifecycle:'SHADOW_CANDIDATE',shadow_evaluations:0,
  shadow_signals:0,shadow_net_pnl_mxn:null,evidence_count:0,
  data_fingerprint:'a'.repeat(64),...over});

const scanner=(over:Partial<ScannerEvidence>={}):ScannerEvidence=>({
  scanner_ran:true,degraded:false,error:null,universe_size:4,scanned_at:'2026-09-19T12:00:00Z',
  score_version:'autofund.market-opportunity.v1',
  eligible:[candidate()],rejected:[candidate({book:'usd_mxn',rank:0,status:'INELIGIBLE_SPREAD',
    reason:'Spread 59 bps exceeds the 100 bps policy',strategy_compatibility:'RESEARCH_ONLY',
    lifecycle:'REJECTED',score:'61.88'})],
  candidates:[candidate(),candidate({book:'usd_mxn',rank:0,status:'INELIGIBLE_SPREAD',
    reason:'Spread 59 bps exceeds the 100 bps policy',strategy_compatibility:'RESEARCH_ONLY',
    lifecycle:'REJECTED',score:'61.88'})],
  shadow:[{market:'btc_mxn',candles:0,evaluations:0,signals:0,signal_rate:'0',net_pnl_mxn:'0',
    strategy_compatibility:'CERTIFIED_FOR_MARKET',evidence_count:0,lifecycle:'RESEARCH_ONLY'}],
  live_market:'btc_mxn',production_market_rotation:'DISABLED',market_promotion:'DISABLED',
  read_only:true,execution_path_to_production:'NOT_PRESENT',interval_seconds:300,status:'HEALTHY',
  fee_source:'ACCOUNT CONFIRMED',last_fee_refresh_at:'2026-09-19T12:00:00Z',
  books_with_account_fee:2,books_with_market_data:2,...over});

const learning=(over:Partial<LearningView>={}):LearningView=>({
  champion:{profile_id:'mean-reversion-safe',strategy_id:'mean_reversion',version:'0.1',
    fingerprint:'f'.repeat(64),certification_status:'CERTIFIED'},
  sessions_observed:1,eligible_evaluations:58,signals:0,
  reason_distribution:{ENTRY_CONDITION_NOT_MET:58},regime_distribution:{NORMAL:58},
  near_signal_count:0,distance_to_signal_min:'0.0031',
  observations:[{classification:'INSUFFICIENT_EVIDENCE',sessions_observed:1,eligible_evaluations:58,
    signals:0,near_signal_count:0,stop_reason:'MAX_SESSION_DURATION_REACHED',
    observations:['ZERO_SIGNALS_ACROSS_ELIGIBLE_EVALUATIONS']}],
  challengers:[],challenger_count:0,promotion:'MANUAL',auto_promotion:'DISABLED',
  market_promotion:'DISABLED',min_sessions_for_challenger:5,min_signals_for_challenger:1,
  scanner:{scanner_ran:true,universe_size:4,eligible_count:2,shadow_evaluations:0,
    candidates:[{market:'btc_mxn',score:'79.92',status:'ELIGIBLE',reason:'ok',
      strategy_compatibility:'CERTIFIED_FOR_MARKET'}]},...over});

test('scanner page shows candidates with status, score and components',()=>{
  render(<MarketScannerPage scanner={scanner()}/>);
  expect(screen.getByText('READ-ONLY RESEARCH')).toBeTruthy();
  expect(screen.getAllByText('btc_mxn').length).toBeGreaterThan(0);
  expect(screen.getByText('RESEARCH CANDIDATE')).toBeTruthy();
  expect(screen.getByText('INELIGIBLE_SPREAD')).toBeTruthy();
  expect(screen.getByText('79.92')).toBeTruthy();
  expect(screen.getByRole('heading',{name:'Score components'})).toBeTruthy();
  expect(screen.getAllByText('movement').length).toBeGreaterThan(0);
});

test('scanner page reports eligible, rejected and universe counts',()=>{
  render(<MarketScannerPage scanner={scanner()}/>);
  expect(screen.getByText('MXN books discovered')).toBeTruthy();
  expect(screen.getByText('Eligible')).toBeTruthy();
  expect(screen.getByText('Rejected')).toBeTruthy();
  expect(screen.getByText('autofund.market-opportunity.v1')).toBeTruthy();
  expect(screen.getByText('ACCOUNT CONFIRMED')).toBeTruthy();
  expect(screen.getByText('HEALTHY')).toBeTruthy();
});

test('scanner page shows research labels and isolation, never trade advice',()=>{
  render(<MarketScannerPage scanner={scanner()}/>);
  expect(screen.getByText(/Scanner ranks research candidates, not financial actions/)).toBeTruthy();
  expect(screen.getByText('NOT PRESENT')).toBeTruthy();
  expect(screen.getByText('FUTURE MANUAL MILESTONE')).toBeTruthy();
  for(const banned of ['BUY NOW','SELL NOW','RECOMMENDED COIN','BEST COIN TO BUY','RECOMMENDED']){
    expect(screen.queryByText(banned)).toBeNull();
  }
  // No control of any kind exists on the scanner page.
  expect(screen.queryAllByRole('button')).toHaveLength(0);
});

test('scanner page marks a degraded scanner without hiding production isolation',()=>{
  render(<MarketScannerPage scanner={scanner({degraded:true,error:'scanner down'})}/>);
  expect(screen.getByText(/MARKET_SCANNER_DEGRADED/)).toBeTruthy();
  expect(screen.getByText(/Production is unaffected/)).toBeTruthy();
});

test('scanner page handles a not-yet-run scanner without fabricating data',()=>{
  render(<MarketScannerPage scanner={scanner({scanner_ran:false,eligible:[],rejected:[],candidates:[],shadow:[]})}/>);
  expect(screen.getByText(/Scanner has not completed a scan yet/)).toBeTruthy();
});

test('scanner exposes no automatic market promotion control',()=>{
  const{container}=render(<MarketScannerPage scanner={scanner()}/>);
  expect(container.querySelectorAll('input,select')).toHaveLength(0);
  expect(screen.queryByText('PROMOTE')).toBeNull();
  expect(screen.queryByText('SET LIVE MARKET')).toBeNull();
});

test('learning page shows champion, evidence and challenger status',()=>{
  render(<LearningPage learning={learning()} activity={[]}/>);
  expect(screen.getByText('mean-reversion-safe')).toBeTruthy();
  expect(screen.getAllByText('CERTIFIED').length).toBeGreaterThan(0);
  expect(screen.getByText('Sessions observed')).toBeTruthy();
  expect(screen.getByText('Eligible evaluations')).toBeTruthy();
  expect(screen.getAllByText('58').length).toBeGreaterThan(0);
  expect(screen.getByText('ENTRY_CONDITION_NOT_MET')).toBeTruthy();
  expect(screen.getAllByText('INSUFFICIENT_EVIDENCE').length).toBeGreaterThan(0);
  expect(screen.getAllByText('MANUAL').length).toBeGreaterThan(0);
  expect(screen.getAllByText('DISABLED').length).toBeGreaterThan(0);
});

test('learning page does not fabricate challengers when evidence is insufficient',()=>{
  render(<LearningPage learning={learning()} activity={[]}/>);
  expect(screen.getByText(/No challenger exists/)).toBeTruthy();
  expect(screen.getByText(/requires 5 sessions/)).toBeTruthy();
});

test('learning page shows the market section with promotion disabled',()=>{
  render(<LearningPage learning={learning()} activity={[]}/>);
  expect(screen.getByRole('heading',{name:'Market opportunity research'})).toBeTruthy();
  expect(screen.getByText(/Promotion of another market to REAL MONEY eligibility/)).toBeTruthy();
  expect(screen.getByText(/future manual milestone/)).toBeTruthy();
});

test('learning page renders a real challenger only when one exists',()=>{
  render(<LearningPage learning={learning({challengers:[{fingerprint:'a'.repeat(64),
    status:'RESEARCH_ONLY',evidence_count:3}],challenger_count:1})} activity={[]}/>);
  expect(screen.getByText('RESEARCH_ONLY')).toBeTruthy();
  expect(screen.getByText('3')).toBeTruthy();
});

test('learning page never offers promotion or live-market controls',()=>{
  const{container}=render(<LearningPage learning={learning()} activity={[]}/>);
  // The only interactive elements belong to the activity filters, never promotion.
  for(const select of container.querySelectorAll('select')){
    expect(select.getAttribute('aria-label')).toMatch(/^Filter by /);
  }
  expect(screen.queryAllByRole('button')).toHaveLength(0);
  for(const banned of ['BUY','SELL','PROMOTE CHALLENGER','ENABLE AUTO-PROMOTION','PROMOTE']){
    expect(screen.queryByText(banned)).toBeNull();
  }
});

test('learning checkpoints table renders the supplied activity',()=>{
  render(<LearningPage learning={learning()} activity={[{event_id:1,event_type:'LEARNING_OBSERVATION',
    timestamp:'2026-09-19T12:00:00Z',summary:'INSUFFICIENT_EVIDENCE',component:'learning',level:'INFO'}]}/>);
  expect(screen.getAllByText('LEARNING_OBSERVATION').length).toBeGreaterThan(0);
  expect(screen.getAllByText('INSUFFICIENT_EVIDENCE').length).toBeGreaterThan(0);
});
