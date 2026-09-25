import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {MvpApp} from './MvpApp';

class Events {addEventListener(){} close(){}}

const stopped:any={product_version:'AutoFund MVP 0.2.3',demo_mode:true,app_state:'STOPPED',auto_execution:false,
 session_id:null,cash_mxn:'50',equity_mxn:'50',deployed_mxn:'0',market_quality:'VALID',accounting_status:'PASS',
 risk_status:'NORMAL',connected:true,kill_triggered:false,position:null,last_signal:'NO_SIGNAL',orders:0,fills:0,
 realized_pnl_mxn:'0',fees_mxn:'0',telemetry:[],champion:{profile_id:'mean-reversion-safe',
 certification_status:'CERTIFIED'},market_regime:'NORMAL',challengers:[],
 production_preflight:{ready:true,label:'READY',reason:'',blockers:[]}};

/** Real 0.2 evidence: real data, and every pair honestly NOT_VIABLE or accumulating. */
const row=(over:any)=>({
 book:'btc_mxn',market:'BTC/MXN',market_class:'VOLATILE_CRYPTO',research_status:'ELIGIBLE',
 strategy_compatibility:'CERTIFIED_FOR_MARKET',evidence_provenance:'REAL_HISTORICAL',
 closed_candles:4323,evaluations:12969,signals:25,shadow_round_trips:25,
 shadow_net_pnl_mxn:'-4.72',shadow_fees_mxn:'1.10',shadow_drawdown_mxn:'3.61',
 economic_reject_rate:'0',certification_state:'NOT_VIABLE',certified:false,
 current_strategy_decision:'NO_SIGNAL',expected_net_edge_bps:null,production_eligible:false,
 position_status:'POSITION_OPEN',reason_code:'POSITION_ALREADY_OPEN_IN_MARKET',
 spread_bps:'6',depth_mxn:'5000',taker_fee:'0.0078',
 fill_model:'DELAYED_NEXT_OBSERVATION',evidence_quality:'CANDLE_ONLY_ESTIMATE',
 experiment_fingerprint:'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2',
 window_provenance:'REAL_HISTORICAL_DEVELOPMENT',certification_reason:'NOT_CERTIFIED',
 executable_fill_model:true,...over});

const view:any={
 research_version:'autofund.production-market-selector.v2',
 certification_version:'autofund.pair-certification.v1',
 universe_version:'autofund.certified-production-universe.v1',
 evidence_store_version:'autofund.research-evidence.v1',
 promotion:'DISABLED',multi_market_production:'DISABLED',production_market:'btc_mxn',
 account_fee_confirmed:true,taker_fee_rate:'0.0078',
 markets:[
  row({}),
  row({book:'eth_mxn',market:'ETH/MXN',position_status:'NO_POSITION',
       reason_code:'NOT_VIABLE'}),
  row({book:'sol_mxn',market:'SOL/MXN',position_status:'NO_POSITION',
       certification_state:'ACCUMULATING_EVIDENCE',reason_code:'INSUFFICIENT_EVIDENCE',
       shadow_round_trips:1,shadow_net_pnl_mxn:'0.0466'}),
  row({book:'usd_mxn',market:'USD/MXN',market_class:'STABLE_OR_FIAT_LIKE',
       position_status:'NO_POSITION',certification_state:'RESEARCH_ONLY',
       reason_code:'FIAT_LIKE_MARKET_EXCLUDED'}),
  row({book:'xrp_mxn',market:'XRP/MXN',position_status:'NO_POSITION',
       certification_state:'CERTIFIED',certified:true,production_eligible:true,
       current_strategy_decision:'BUY',expected_net_edge_bps:'180',
       reason_code:'CURRENT_OPPORTUNITY',shadow_net_pnl_mxn:'0.14'}),
 ],
 certified_markets:['XRP/MXN'],selectable_markets:['XRP/MXN'],
 selection_outcome:'CANDIDATE',one_unresolved_order_globally:true,
 llm_influences_selection:false};

beforeEach(()=>{vi.stubGlobal('EventSource',Events);vi.stubGlobal('fetch',vi.fn().mockResolvedValue({json:async()=>({control_token:'token'})}))});
afterEach(()=>{cleanup();vi.unstubAllGlobals()});

const open=()=>fireEvent.click(screen.getByRole('button',{name:'Markets'}));

test('production markets lists every market with its evidence',()=>{
 render(<MvpApp initial={{...stopped,production_markets:view}}/>);
 open();
 expect(screen.getByText('Production markets')).toBeTruthy();
 for(const market of ['BTC/MXN','ETH/MXN','SOL/MXN','USD/MXN','XRP/MXN'])
   expect(document.querySelector(`[data-market="${market}"]`)).toBeTruthy();
 expect(screen.getAllByText('REAL_HISTORICAL').length).toBeGreaterThan(0);
});

test('every non-trading market shows a deterministic reason, never a blank',()=>{
 render(<MvpApp initial={{...stopped,production_markets:view}}/>);
 open();
 // Each refusal must be attributable rather than an ambiguous "not selected".
 for(const reason of ['POSITION_ALREADY_OPEN_IN_MARKET','NOT_VIABLE',
   'INSUFFICIENT_EVIDENCE','FIAT_LIKE_MARKET_EXCLUDED','CURRENT_OPPORTUNITY'])
   expect(document.querySelector(`[data-reason="${reason}"]`)).toBeTruthy();
 for(const banned of ['NOT SELECTED','UNKNOWN'])
   expect(screen.queryByText(banned)).toBeNull();
});

test('visual states distinguish research, accumulating, viable and position open',()=>{
 render(<MvpApp initial={{...stopped,production_markets:view}}/>);
 open();
 const states=['ACCUMULATING','NOT VIABLE','POSITION OPEN','CURRENT OPPORTUNITY'];
 for(const state of states){
   const nodes=document.querySelectorAll(`[data-state="${state}"]`);
   expect(nodes.length).toBeGreaterThan(0);
 }
});

test('certification is shown per market and profile, not globally',()=>{
 render(<MvpApp initial={{...stopped,production_markets:view}}/>);
 open();
 const certified=document.querySelectorAll('[data-certification="CERTIFIED"]');
 expect(certified.length).toBe(1);
 expect(document.querySelector('[data-market="XRP/MXN"]')).toBeTruthy();
});

test('fiat-like markets are visibly excluded from volatile crypto research',()=>{
 render(<MvpApp initial={{...stopped,production_markets:view}}/>);
 open();
 expect(document.querySelector('[data-market="USD/MXN"]')?.getAttribute('data-reason'))
   .toBe('FIAT_LIKE_MARKET_EXCLUDED');
 expect(screen.getByText('STABLE_OR_FIAT_LIKE')).toBeTruthy();
});

test('multi-market production and promotion are shown as disabled',()=>{
 render(<MvpApp initial={{...stopped,production_markets:view}}/>);
 open();
 expect(screen.getByText(/Multi-market Production DISABLED/)).toBeTruthy();
 expect(screen.getByText(/Promotion DISABLED/)).toBeTruthy();
});

test('the page states that no language model influences selection',()=>{
 render(<MvpApp initial={{...stopped,production_markets:view}}/>);
 open();
 expect(screen.getByText(/No language model/)).toBeTruthy();
 expect(screen.getByText('LLM influences selection')).toBeTruthy();
});

test('the page exposes no manual trading or promotion control',()=>{
 render(<MvpApp initial={{...stopped,production_markets:view}}/>);
 open();
 for(const name of ['BUY','SELL','PROMOTE','ACTIVATE','ENABLE MARKET','FORCE TRADE','SET LIVE MARKET'])
   expect(screen.queryByRole('button',{name})).toBeNull();
});

test('missing production market evidence is stated rather than fabricated',()=>{
 render(<MvpApp initial={{...stopped}}/>);
 open();
 expect(screen.getByText('Production markets')).toBeTruthy();
 expect(screen.getByText(/No production market evidence published yet/)).toBeTruthy();
});

test('every pair is shown with the fill model that produced its evidence',()=>{
 render(<MvpApp initial={{...stopped,production_markets:view}}/>);
 open();
 // A round-trip count is only interpretable alongside its fill model: five trades
 // under a same-bar close fill and five under a delayed executable fill are
 // different claims, and a table showing only the count invites the wrong reading.
 const cells=document.querySelectorAll('[data-fill-model]');
 expect(cells.length).toBe(view.markets.length);
 for(const cell of Array.from(cells))
   expect(cell.getAttribute('data-fill-model')).toBe('DELAYED_NEXT_OBSERVATION');
 expect(screen.queryByText('SAME_BAR_CLOSE')).toBeNull();
});

test('fill evidence quality is labelled so an estimate is not read as a book',()=>{
 render(<MvpApp initial={{...stopped,production_markets:view}}/>);
 open();
 const cells=document.querySelectorAll('[data-evidence-quality]');
 expect(cells.length).toBe(view.markets.length);
 for(const cell of Array.from(cells))
   expect(cell.getAttribute('data-evidence-quality')).toBe('CANDLE_ONLY_ESTIMATE');
});

test('development evidence is labelled as development, not as validation',()=>{
 render(<MvpApp initial={{...stopped,production_markets:view}}/>);
 open();
 const cells=document.querySelectorAll('[data-window-provenance]');
 expect(cells.length).toBe(view.markets.length);
 for(const cell of Array.from(cells))
   expect(cell.getAttribute('data-window-provenance')).toBe('REAL_HISTORICAL_DEVELOPMENT');
 // The panel must never imply that this window confirms anything out of sample.
 expect(screen.queryByText(/out-of-sample/i)).toBeNull();
 expect(screen.queryByText(/HOLDOUT VALIDATED/i)).toBeNull();
});

test('the experiment fingerprint is shown per pair and quoted in full',()=>{
 render(<MvpApp initial={{...stopped,production_markets:view}}/>);
 open();
 const shown=screen.getAllByText(view.markets[0].experiment_fingerprint);
 expect(shown.length).toBe(view.markets.length);
 expect(view.markets[0].experiment_fingerprint.length).toBe(64);
});

test('a certification refusal states its reason per pair',()=>{
 render(<MvpApp initial={{...stopped,production_markets:view}}/>);
 open();
 const cells=document.querySelectorAll('[data-certification-reason]');
 expect(cells.length).toBe(view.markets.length);
 for(const cell of Array.from(cells))
   expect(cell.getAttribute('data-certification-reason')).not.toBe('—');
});

test('the page never ranks markets or names a best coin',()=>{
 render(<MvpApp initial={{...stopped,production_markets:view}}/>);
 open();
 for(const banned of [/best coin/i,/most profitable/i,/top market/i,/recommended market/i])
   expect(screen.queryByText(banned)).toBeNull();
});
