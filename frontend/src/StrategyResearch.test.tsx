import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {MvpApp} from './MvpApp';

class Events {addEventListener(){} close(){}}

const stopped:any={product_version:'AutoFund MVP 0.2.3',demo_mode:true,app_state:'STOPPED',auto_execution:false,
 session_id:null,cash_mxn:'50',equity_mxn:'50',deployed_mxn:'0',market_quality:'VALID',accounting_status:'PASS',
 risk_status:'NORMAL',connected:true,kill_triggered:false,position:null,last_signal:'NO_SIGNAL',orders:0,fills:0,
 realized_pnl_mxn:'0',fees_mxn:'0',telemetry:[],champion:{profile_id:'mean-reversion-safe',strategy_id:'mean_reversion',
 version:'0.1',fingerprint:'5a47c9833724e58d64ef37e8702aafaf2e503940e91ad17aa626a72ac53865a8',
 certification_status:'CERTIFIED'},market_regime:'NORMAL',challengers:[],
 production_preflight:{ready:true,label:'READY',reason:'',blockers:[]}};

/** The real economics: a 20 bps Champion target against ~156 bps of round-trip fee. */
const research:any={research_version:'autofund.production-market-selector.v1',
 promotion:'DISABLED',multi_market_production:'DISABLED',
 champion:{profile_id:'mean-reversion-safe',strategy_id:'mean_reversion',version:'0.1',
  fingerprint:'5a47c9833724e58d64ef37e8702aafaf2e503940e91ad17aa626a72ac53865a8',
  certification_status:'CERTIFIED',lifecycle:'ACTIVE_PRODUCTION',intended_gross_edge_bps:'20'},
 profiles:[
  {profile_id:'mean-reversion-safe-v1',strategy_id:'mean_reversion',version:'0.1',
   parameters:{},target_model:{version:'autofund.volatility-aware-target.v1',atr_multiple:'1.5',
   floor_bps:'60',cap_bps:'1200'},fingerprint:'a'.repeat(64),strategy_fingerprint:'b'.repeat(64),
   profile_contract_version:'autofund.strategy-profile.v2',markets:['btc_mxn'],evaluator:'MeanReversionSafeV1',
   description:'frozen',markets_certified:['btc_mxn'],lifecycle:'ACTIVE_PRODUCTION'},
  {profile_id:'trend-continuation-v1',strategy_id:'trend_continuation',version:'0.1',
   parameters:{},target_model:{version:'autofund.volatility-aware-target.v1',atr_multiple:'2.0',
   floor_bps:'250',cap_bps:'1500'},fingerprint:'c'.repeat(64),strategy_fingerprint:'d'.repeat(64),
   profile_contract_version:'autofund.strategy-profile.v2',markets:[],evaluator:'TrendContinuationV1',
   description:'research challenger',markets_certified:[],lifecycle:'RESEARCH'},
  {profile_id:'volatility-mean-reversion-v1',strategy_id:'volatility_mean_reversion',version:'0.1',
   parameters:{},target_model:{version:'autofund.volatility-aware-target.v1',atr_multiple:'1.5',
   floor_bps:'120',cap_bps:'900'},fingerprint:'e'.repeat(64),strategy_fingerprint:'f'.repeat(64),
   profile_contract_version:'autofund.strategy-profile.v2',markets:[],evaluator:'VolatilityAwareMeanReversionV1',
   description:'research challenger',markets_certified:[],lifecycle:'RESEARCH'}],
 economic_viability:{taker_fee_rate:'0.00780000',spread_bps:'10',account_fee_confirmed:true,
  minimum_viable_gross_edge_bps:'165.39160000',champion_intended_gross_edge_bps:'20',
  champion_economically_viable:false},
 markets:[{market:'btc_mxn',status:'ELIGIBLE',strategy_compatibility:'CERTIFIED_FOR_MARKET',
  market_class:'VOLATILE_CRYPTO',lifecycle:'RESEARCH_ONLY'},
  {market:'usd_mxn',status:'ELIGIBLE',strategy_compatibility:'RESEARCH_ONLY',
  market_class:'STABLE_OR_FIAT_LIKE',lifecycle:'RESEARCH_ONLY'}],
 shadow:[{market:'BTC/MXN',evaluations:156,signals:2,shadow_trades:1,gross_pnl_mxn:'0.89',
  fees_mxn:'0.09',net_pnl_mxn:'0.80',max_drawdown_mxn:'0',economic_reject_rate:'0.5',
  data_source:'CAPTURED_MARKET_DATA',evaluated:true,profiles:[{profile_id:'trend-continuation-v1'}]}],
 profile_by_id:['mean-reversion-safe-v1','trend-continuation-v1','volatility-mean-reversion-v1'],
 frozen_profiles:['mean-reversion-safe-v1','trend-continuation-v1',
  'volatility-mean-reversion-v1'],
 risk_adjusted_challengers:[
  {profile_id:'volatility-mean-reversion-v2',strategy_id:'volatility_mean_reversion',
   version:'0.2',parameters:{max_holding_bars:'240'},
   target_model:{version:'autofund.volatility-aware-target.v1',atr_multiple:'2.0',
   floor_bps:'250',cap_bps:'1200'},fingerprint:'1'.repeat(64),strategy_fingerprint:'2'.repeat(64),
   profile_contract_version:'autofund.strategy-profile.v2',markets:[],
   evaluator:'VolatilityMeanReversionV2',description:'risk-bounded mean reversion',
   markets_certified:[],lifecycle:'RESEARCH',
   risk_adjusted_metrics:true,declares_invalidation:true},
  {profile_id:'range-expansion-v1',strategy_id:'range_expansion',version:'0.1',
   parameters:{max_holding_bars:'180'},
   target_model:{version:'autofund.volatility-aware-target.v1',atr_multiple:'2.5',
   floor_bps:'300',cap_bps:'1800'},fingerprint:'3'.repeat(64),strategy_fingerprint:'4'.repeat(64),
   profile_contract_version:'autofund.strategy-profile.v2',markets:[],
   evaluator:'RangeExpansionV1',description:'confirmed range expansion',
   markets_certified:[],lifecycle:'RESEARCH',
   risk_adjusted_metrics:true,declares_invalidation:true}],
 risk_adjusted_research:{selection_rule:['risk_gates_satisfied',
  'mae_to_reward_ratio_ascending','effective_drawdown_ascending','net_pnl_descending'],
  configuration_sets:{'volatility-mean-reversion-v2':4,'range-expansion-v1':4},
  mae_mfe_implemented:true,markets_pooled:false,holdout_frozen_before_selection:true,
  drawdown_policy_changed:false,capital_limits_changed:false},
 execution_research:{maker_fee_rate:'0.00600000',taker_fee_rate:'0.00780000',
  maker_fee_confirmed:true,maker_below_taker:true,
  modes:{
   TAKER_TAKER:{mode:'TAKER_TAKER',entry_liquidity:'TAKER',exit_liquidity:'TAKER',
    entry_order_type:'MARKET',exit_order_type:'MARKET',timeout_bars:1,
    requires_post_only:false,research_only:true,production_authorised:false,
    description:'baseline'},
   MAKER_MAKER:{mode:'MAKER_MAKER',entry_liquidity:'MAKER',exit_liquidity:'MAKER',
    entry_order_type:'LIMIT_POST_ONLY',exit_order_type:'LIMIT_POST_ONLY',timeout_bars:5,
    requires_post_only:true,research_only:true,production_authorised:false,
    description:'both legs passive'}},
  fee_floors:{TAKER_TAKER:'157.8444',MAKER_MMAKER:'121.0887'},
  fill_evidence:'CANDLE_ONLY_UNCERTAIN',maker_fills_determined:false,
  research_only:true,production_authorised:false,cancel_implemented:false,
  production_mutations_added:0},
 production_gap:{missing:['cancel_capability','stale_order_handling'],
  partial:['limit_order_submission','price_field'],
  present:['open_order_monitoring','one_unresolved_order_invariant'],
  production_mutations_required:['limit_order_submission','cancel_capability']}};

beforeEach(()=>{vi.stubGlobal('EventSource',Events);vi.stubGlobal('fetch',vi.fn().mockResolvedValue({json:async()=>({control_token:'token'})}))});
afterEach(()=>{cleanup();vi.unstubAllGlobals()});

const openProfiles=()=>fireEvent.click(screen.getByRole('button',{name:'Profiles'}));

test('strategy research shows the champion as the only active production profile',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Strategy research')).toBeTruthy();
 // Only the Champion is ACTIVE PRODUCTION; the challengers are RESEARCH.
 expect(screen.getAllByText('ACTIVE PRODUCTION').length).toBeGreaterThan(0);
 expect(screen.getAllByText('RESEARCH').length).toBeGreaterThan(0);
 expect(screen.getAllByText('mean-reversion-safe-v1').length).toBeGreaterThan(0);
});

test('the champion is reported not economically viable with the reason shown',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Champion economic viability')).toBeTruthy();
 expect(screen.getByText(/refused by the economic guard/)).toBeTruthy();
 expect(screen.getByText('20')).toBeTruthy();
 expect(screen.getByText('165.39160000')).toBeTruthy();
});

test('profiles are listed with their lifecycle, targets and fingerprints',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 for(const id of ['trend-continuation-v1','volatility-mean-reversion-v1'])
   expect(screen.getByText(id)).toBeTruthy();
 expect(screen.getByText('250 bps')).toBeTruthy();
 expect(screen.getByText('1500 bps')).toBeTruthy();
});

test('shadow research is reported per market with friction separated from net',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Shadow research per market')).toBeTruthy();
 expect(screen.getByText('BTC/MXN')).toBeTruthy();
 expect(screen.getByText('156')).toBeTruthy();
 expect(screen.getByText('0.80')).toBeTruthy();
 expect(screen.getByText('CAPTURED_MARKET_DATA')).toBeTruthy();
});

test('fiat-like markets are visibly classified and excluded from volatile research',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('usd_mxn')).toBeTruthy();
 expect(screen.getByText('STABLE_OR_FIAT_LIKE')).toBeTruthy();
});

test('promotion and multi-market production are shown as disabled',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText(/Promotion DISABLED/)).toBeTruthy();
 expect(screen.getByText(/Multi-market Production DISABLED/)).toBeTruthy();
});

test('the research page exposes no manual trading control',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 for(const name of ['BUY','SELL','PLACE ORDER','PROMOTE','ACTIVATE','ENABLE','FORCE TRADE'])
   expect(screen.queryByRole('button',{name})).toBeNull();
});

test('missing research evidence is stated rather than fabricated',()=>{
 render(<MvpApp initial={{...stopped}}/>);
 openProfiles();
 expect(screen.getByText('Strategy research')).toBeTruthy();
 expect(screen.getByText(/No strategy research evidence published yet/)).toBeTruthy();
});

test('risk-adjusted challengers are shown with their declared risk boundary',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Risk-adjusted challengers')).toBeTruthy();
 for(const id of ['volatility-mean-reversion-v2','range-expansion-v1'])
   expect(document.querySelector(`[data-challenger="${id}"]`)).toBeTruthy();
 // v1 is deliberately NOT listed among the risk-bounded challengers: it declares no
 // boundary, and showing it here would imply a risk claim it does not make.
 expect(document.querySelector('[data-challenger="volatility-mean-reversion-v1"]')).toBeNull();
});

test('a challenger without a declared boundary is labelled rather than implied safe',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 const rows=document.querySelectorAll('[data-challenger]');
 expect(rows.length).toBe(2);
 for(const row of Array.from(rows))
   expect(row.getAttribute('data-declares-invalidation')).toBe('true');
});

test('the research page reports MAE/MFE and that markets are not pooled',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-mae-mfe="true"]')).toBeTruthy();
 expect(document.querySelector('[data-pooled="false"]')).toBeTruthy();
});

test('the selection rule is shown and is risk-first',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText(/risk_gates_satisfied/)).toBeTruthy();
 const shown=screen.getByText(/risk_gates_satisfied/).textContent??'';
 expect(shown.indexOf('risk_gates_satisfied'))
   .toBeLessThan(shown.indexOf('net_pnl_descending'));
});

test('the research page states that policy and capital limits are unchanged',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-policy-changed="false"]')).toBeTruthy();
 expect(document.querySelector('[data-capital-changed="false"]')).toBeTruthy();
});

test('passive execution research is shown with the account-confirmed maker fee',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Passive execution economics')).toBeTruthy();
 expect(document.querySelector('[data-maker-confirmed="true"]')).toBeTruthy();
 expect(document.querySelector('[data-maker-cheaper="true"]')).toBeTruthy();
});

test('the fill evidence class is shown and maker fills are not claimed',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-fill-evidence="CANDLE_ONLY_UNCERTAIN"]')).toBeTruthy();
 // Candle history cannot support a maker fill, so the page must not imply one was found.
 expect(document.querySelector('[data-maker-fills="false"]')).toBeTruthy();
});

test('passive Production is shown as not authorised and cancel as not implemented',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-production-authorised="false"]')).toBeTruthy();
 expect(document.querySelector('[data-cancel-implemented="false"]')).toBeTruthy();
});

test('every execution mode is priced and its post-only requirement is visible',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 const rows=document.querySelectorAll('[data-mode]');
 expect(rows.length).toBe(2);
 const postOnly=Array.from(rows)
   .filter(row=>row.getAttribute('data-requires-post-only')==='true');
 expect(postOnly.length).toBe(1);
 expect(postOnly[0].getAttribute('data-mode')).toBe('MAKER_MAKER');
});

test('the Production architecture gap is reported with its required mutations',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Production architecture gap')).toBeTruthy();
 expect(document.querySelector('[data-gap-missing="2"]')).toBeTruthy();
 // Cancellation is the security blocker and must be visible as a required mutation.
 const mutations=document.querySelector('[data-gap-mutations="2"]');
 expect(mutations).toBeTruthy();
 expect(mutations?.textContent).toContain('cancel_capability');
});
