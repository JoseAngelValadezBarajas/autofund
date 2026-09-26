import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {MvpApp} from './MvpApp';

class Events {addEventListener(){} close(){}}

const stopped:any={product_version:'AutoFund MVP 0.2.8',demo_mode:true,app_state:'STOPPED',auto_execution:false,
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
  production_mutations_required:['limit_order_submission','cancel_capability']},
 horizon_research:{hypothesis:'coarser horizons may make friction a smaller share of the opportunity',
  predeclared_timeframes:['15m','1h'],horizons_tested_by_rule:true,
  profiles:[
   {profile_id:'volatility-mean-reversion-15m-v1',strategy_id:'volatility_mean_reversion_horizon',
    version:'0.1',fingerprint:'a'.repeat(64),strategy_fingerprint:'b'.repeat(64),
    timeframe:{version:'autofund.time-horizon.v1',name:'15m',seconds:900,base_bars:15,
     label_convention:'BUCKET_OPEN'},
    concept:'volatility_mean_reversion',horizon_profile_version:'autofund.horizon-profile.v1',
    description:'mean reversion on 15m'},
   {profile_id:'volatility-mean-reversion-1h-v1',strategy_id:'volatility_mean_reversion_horizon',
    version:'0.1',fingerprint:'c'.repeat(64),strategy_fingerprint:'d'.repeat(64),
    timeframe:{version:'autofund.time-horizon.v1',name:'1h',seconds:3600,base_bars:60,
     label_convention:'BUCKET_OPEN'},
    concept:'volatility_mean_reversion',horizon_profile_version:'autofund.horizon-profile.v1',
    description:'mean reversion on 1h'},
   {profile_id:'range-expansion-15m-v1',strategy_id:'range_expansion_horizon',
    version:'0.1',fingerprint:'e'.repeat(64),strategy_fingerprint:'f'.repeat(64),
    timeframe:{version:'autofund.time-horizon.v1',name:'15m',seconds:900,base_bars:15,
     label_convention:'BUCKET_OPEN'},
    concept:'range_expansion',horizon_profile_version:'autofund.horizon-profile.v1',
    description:'range expansion on 15m'},
   {profile_id:'range-expansion-1h-v1',strategy_id:'range_expansion_horizon',
    version:'0.1',fingerprint:'7'.repeat(64),strategy_fingerprint:'8'.repeat(64),
    timeframe:{version:'autofund.time-horizon.v1',name:'1h',seconds:3600,base_bars:60,
     label_convention:'BUCKET_OPEN'},
    concept:'range_expansion',horizon_profile_version:'autofund.horizon-profile.v1',
    description:'range expansion on 1h'}],
  profile_ids:['volatility-mean-reversion-15m-v1','volatility-mean-reversion-1h-v1',
   'range-expansion-15m-v1','range-expansion-1h-v1'],
  max_configurations_per_strategy_and_horizon:4,
  configuration_budget:{'volatility_mean_reversion:15m':4,'volatility_mean_reversion:1h':4,
   'range_expansion:15m':4,'range_expansion:1h':4},
  selection_rule:['risk_gates_satisfied','friction_ratio_ascending',
   'net_pnl_per_capital_hour_descending','net_pnl_descending'],
  all_configurations_retained:true,certifying_execution_mode:'TAKER_TAKER',
  maker_bound_may_certify:false,base_bar_seconds:60,
  aggregation_uses_existing_base_bars:true,incomplete_buckets_dropped_not_filled:true,
  same_bar_fills_allowed:false,
  outcomes:['LONGER_HORIZON_PROMISING','NO_HORIZON_EDGE','INSUFFICIENT_HORIZON_EVIDENCE','BLOCKED'],
  verdict:null,verdict_pending_frozen_experiment:true,
  frozen_profile_fingerprints_changed:false,drawdown_policy_changed:false,
  capital_limits_changed:false},
 microstructure_capture:{enabled:true,provenance:'REAL_CAPTURED_MICROSTRUCTURE',
  endpoints:['order_book','trades'],order_capability:'NONE',production_mutation:'NONE',
  credentials_scope:'READ_ONLY_PUBLIC_ENDPOINTS',separate_from_production_ledger:true,
  captured_books:['btc_mxn','eth_mxn','sol_mxn','xrp_mxn'],order_book_events:120,
  trade_tape_events:340,capture_seconds:3600,health:'HEALTHY',gaps:0,duplicates:2,
  sequence_regressions:0,anomalies_repaired:0,bounded_storage:true,retention_hours:72,
  queue_position_observable:false,passive_fill_exactness:'BOUNDED_ONLY',
  adverse_selection_analysis_possible:true,exact_passive_fill_claim_made:false},
 asymmetry_research:{hypothesis:'entries near a genuine invalidation may improve geometry',
  challengers:[
   {profile_id:'structural-invalidation-pullback-15m-PB-A-v1',
    strategy_id:'structural_invalidation_pullback',version:'0.1',
    fingerprint:'9'.repeat(64),strategy_fingerprint:'a'.repeat(64),
    concept:'structural_invalidation_pullback',
    asymmetric_challenger_version:'autofund.asymmetric-challenger.v1',
    description:'pullback entry',
    timeframe:{name:'15m',seconds:900,base_bars:15,label_convention:'BUCKET_OPEN'},
    parameters:{max_risk_bps:'120',max_holding_bars:'16'}},
   {profile_id:'expansion-retest-1h-RT-A-v1',strategy_id:'expansion_retest',version:'0.1',
    fingerprint:'b'.repeat(64),strategy_fingerprint:'c'.repeat(64),concept:'expansion_retest',
    asymmetric_challenger_version:'autofund.asymmetric-challenger.v1',
    description:'retest entry',
    timeframe:{name:'1h',seconds:3600,base_bars:60,label_convention:'BUCKET_OPEN'},
    parameters:{max_risk_bps:'120',max_holding_bars:'24'}}],
  challenger_ids:['structural-invalidation-pullback-15m-PB-A-v1',
   'expansion-retest-1h-RT-A-v1'],
  max_configurations_per_challenger:4,
  configuration_budget:{'structural_invalidation_pullback:15m':4,
   'structural_invalidation_pullback:1h':4,'expansion_retest:15m':4,'expansion_retest:1h':4},
  reward_risk_thresholds:['0.5','0.75','1.0','2.0'],
  threshold_basis:'band derived from the unchanged gate, which nets friction from both paths',
  selection_rule:['risk_gates_satisfied','median_mfe_to_mae_descending',
   'median_net_to_mae_descending','net_pnl_descending'],
  comparison_semantics:['risk_gates_satisfied_on_holdout',
   'median_mfe_to_mae_strictly_greater_than_predecessor',
   'median_net_to_mae_not_worse_than_predecessor','holdout_net_pnl_non_negative'],
  all_configurations_retained:true,thresholds_chosen_before_results:true,
  certifying_execution_mode:'TAKER_TAKER',maker_bound_computed:false,
  maker_bound_reason:'maker fills are unobservable from candle history',
  gate_is_research_only:true,gate_can_override_economic_guard:false,
  gate_can_override_risk_engine:false,gate_can_override_risk_policy:false,
  no_future_structure:true,verdict:null,verdict_pending_frozen_experiment:true,
  drawdown_policy_changed:false,risk_engine_changed:false,capital_limits_changed:false},
 alpha_research:{question:'does an independent information source exist with predictive content',
  alpha_is_information_not_pnl:true,
  price_only_baseline_label:'PRICE_ONLY_RESEARCH_BASELINE',
  price_only_baseline_frozen:true,frozen_price_only_profiles:25,
  new_price_only_strategy_implemented:false,
  source_families:['CROSS_MARKET_LEAD_LAG','MARKET_MICROSTRUCTURE_ORDER_FLOW'],
  predeclared_features:['LAGGED_RETURN','RELATIVE_RETURN_VS_BASKET',
   'CROSS_SECTIONAL_DISPERSION','LEADER_FOLLOWER_DIVERGENCE',
   'VOLATILITY_ADJUSTED_RELATIVE_MOVE','MARKET_BREADTH'],
  feature_interpretation:{LAGGED_RETURN:'a leader market return over the preceding bar',
   RELATIVE_RETURN_VS_BASKET:'a market return relative to the equally weighted basket',
   CROSS_SECTIONAL_DISPERSION:'dispersion of returns across the tracked markets',
   LEADER_FOLLOWER_DIVERGENCE:'how far a follower has diverged from its leader',
   VOLATILITY_ADJUSTED_RELATIVE_MOVE:'relative move scaled by recent volatility',
   MARKET_BREADTH:'share of tracked markets moving in the same direction'},
  predeclared_relationships:[['BTC/MXN','ETH/MXN'],['BTC/MXN','SOL/MXN'],
   ['BTC/MXN','XRP/MXN'],['ETH/MXN','SOL/MXN']],
  predeclared_horizons_minutes:[1,5,15],predeclared_markout_seconds:[5,30,60],
  max_feature_families:6,max_relationships:12,
  parameters_declared_before_results:true,multiple_testing_budget_enforced:true,
  negative_controls:['TIME_SHUFFLED','RANDOMISED_PAIRING','FUTURE_LEAK','SIGN_INVERSION'],
  negative_controls_run_on_every_measurement:true,
  positive_control_required_for_a_verdict:true,
  undetectable_measurements_reported_as_unmeasurable:true,
  verdicts:['PREDICTIVE','NOT_PREDICTIVE','INSUFFICIENT_SAMPLE'],
  classifications:['NO_SIGNAL','WEAK_UNSTABLE_SIGNAL','PREDICTIVE_NOT_ECONOMIC',
   'VALIDATED_ALPHA_SOURCE','MICROSTRUCTURE_ACCUMULATING','INSUFFICIENT_EVIDENCE'],
  terminal_statuses:['VALIDATED_ALPHA_SOURCE_FOUND','PREDICTIVE_BUT_NOT_ECONOMIC',
   'MICROSTRUCTURE_ACCUMULATING','NO_ALPHA_SOURCE_FOUND','INSUFFICIENT_EVIDENCE','BLOCKED'],
  development_only_during_discovery:true,validation_read_only_after_freeze:true,
  holdout_preserved:true,holdout_consumed:false,no_future_structure:true,
  discovery_pipeline_may_report_absence:true,
  gate_can_override_economic_guard:false,gate_can_override_risk_engine:false,
  gate_can_override_risk_policy:false,new_strategy_implemented:false,
  maker_authorised:false,verdict:null,verdict_pending_frozen_experiment:true,
  drawdown_policy_changed:false,risk_engine_changed:false,capital_limits_changed:false},
 venue_economics:{question:'is the product thesis economically feasible anywhere',
  is_a_decision_milestone:true,builds_a_strategy:false,builds_a_venue_adapter:false,
  frozen_alpha_label:'VALIDATED_INFORMATION_SIGNAL_V1',
  frozen_alpha_fingerprint:'fae4faa85ef648acb5e831cb6bc3fe242d8a1723cc3c474262a886a4600ab3aa',
  frozen_alpha_movement_bps:'2.513590292581896613688157726',
  frozen_alpha_retuned_for_venue_economics:false,
  capture_scenarios:['100%','75%','50%','25%'],
  execution_modes:['TAKER_TAKER','MAKER_TAKER','MAKER_MAKER'],
  verdicts:['CLEARLY_NOT_ECONOMIC','THEORETICALLY_POSSIBLE_ONLY_UNDER_PASSIVE_EXECUTION',
   'POTENTIALLY_ECONOMIC','INSUFFICIENT_COST_EVIDENCE'],
  early_fail_classification:'STRUCTURALLY_UNTRADEABLE_FOR_THIS_ALPHA',
  feasibility_classifications:['CURRENT_VENUE_VIABLE','LOWER_COST_VENUE_REQUIRED',
   'NEW_ALPHA_SOURCE_REQUIRED','MICROSTRUCTURE_EVIDENCE_PENDING',
   'ACTIVE_TRADING_THESIS_NOT_SUPPORTED'],
  venue_count:4,
  venues:[
   {venue:'Bitso',accessibility:'VERIFIED',spot_api:true,public_market_data_api:true,
    authenticated_trading_api:true,supports_market_orders:true,supports_limit:true,
    supports_post_only:true,supports_client_order_id:true,order_status_api:true,
    fills_api:true,open_orders_api:true,cancel_api:true,websocket_support:true,
    order_book_api:true,trade_tape_api:true,minimum_order_quote:'10',
    minimum_order_currency:'MXN',mxn_quote_pairs:true,
    mxn_pair_symbols:['btc_mxn','eth_mxn'],fee_tier_requirements:'volume-tiered',
    fee_currency_semantics:'BUY_FEE_IN_BASE;SELL_FEE_IN_QUOTE',rate_limits:'documented',
    liquidity_evidence:'12 MXN books',recovery_feasibility:'origin_id supported',
    migration_effort:'LOW',notes:'incumbent'},
   {venue:'Binance',accessibility:'VERIFIED',spot_api:true,public_market_data_api:true,
    authenticated_trading_api:true,supports_market_orders:true,supports_limit:true,
    supports_post_only:true,supports_client_order_id:true,order_status_api:true,
    fills_api:true,open_orders_api:true,cancel_api:true,websocket_support:true,
    order_book_api:true,trade_tape_api:true,minimum_order_quote:'150',
    minimum_order_currency:'MXN',mxn_quote_pairs:true,mxn_pair_symbols:['BTCMXN'],
    fee_tier_requirements:'Regular User',fee_currency_semantics:'received asset',
    rate_limits:'documented',liquidity_evidence:'deep on BTC',
    recovery_feasibility:'clientOrderId',migration_effort:'HIGH',
    notes:'minNotional 150 MXN'},
   {venue:'Kraken',accessibility:'VERIFIED',spot_api:true,public_market_data_api:true,
    authenticated_trading_api:true,supports_market_orders:true,supports_limit:true,
    supports_post_only:true,supports_client_order_id:true,order_status_api:true,
    fills_api:true,open_orders_api:true,cancel_api:true,websocket_support:true,
    order_book_api:true,trade_tape_api:true,minimum_order_quote:'0.5',
    minimum_order_currency:'USD',mxn_quote_pairs:false,mxn_pair_symbols:[],
    fee_tier_requirements:'Tier 1 entry',fee_currency_semantics:'quote volume',
    rate_limits:'documented',liquidity_evidence:'deep USD only',
    recovery_feasibility:'documented',migration_effort:'HIGH',
    notes:'no MXN pair'},
   {venue:'Coinbase Advanced',accessibility:'VERIFIED',spot_api:true,
    public_market_data_api:true,authenticated_trading_api:true,
    supports_market_orders:true,supports_limit:true,supports_post_only:true,
    supports_client_order_id:true,order_status_api:true,fills_api:true,
    open_orders_api:true,cancel_api:true,websocket_support:true,order_book_api:true,
    trade_tape_api:true,minimum_order_quote:null,minimum_order_currency:null,
    mxn_quote_pairs:false,mxn_pair_symbols:[],fee_tier_requirements:'requires sign-in',
    fee_currency_semantics:'not established',rate_limits:'not established',
    liquidity_evidence:'no MXN product',recovery_feasibility:'unreachable in MXN',
    migration_effort:'HIGH',notes:'fees unknown'}],
  referenced_venue_data:{version:'autofund.venue-data.v1',retrieved_at:'2026-09-25',
   sources:[
    {venue:'Bitso',maker_rate:'0.6',taker_rate:'0.78',basis:'ACCOUNT_CONFIRMED',
     source:'docs.bitso.com',retrieved_at:'2026-09-25',rates_known:true,notes:'account'},
    {venue:'Binance',maker_rate:'0.1',taker_rate:'0.1',basis:'PUBLISHED_BASELINE',
     source:'binance.com/en/fee/schedule',retrieved_at:'2026-09-25',rates_known:true,
     notes:'Regular User'},
    {venue:'Kraken',maker_rate:'0.4',taker_rate:'0.8',basis:'PUBLISHED_BASELINE',
     source:'kraken.com/features/fee-schedule',retrieved_at:'2026-09-25',rates_known:true,
     notes:'Tier 1'},
    {venue:'Coinbase Advanced',maker_rate:null,taker_rate:null,basis:'ACCOUNT_RATE_UNKNOWN',
     source:'help.coinbase.com',retrieved_at:'2026-09-25',rates_known:false,
     notes:'requires sign-in'}],
   account_rate_unknown_venues:['Coinbase Advanced']},
  established_fee_floors_bps:{TAKER_TAKER:'157.8443689034903612824254100',
   MAKER_TAKER:'139.4498821187556704873465700',
   MAKER_MAKER:'121.0887052698484670599047000'},
  fee_conventions_available:['GEOMETRIC_COMPOUNDED','DOUBLED_RATE'],
  fee_convention_note:'two conventions differ by about 1.8 bps',
  historical_friction_bps:'173.0000',authorized_capital_mxn:'50',
  max_deployment_mxn:'25',max_single_order_mxn:'11',
  minimum_order_compatibility_values:['COMPATIBLE_WITH_CURRENT_EXPERIMENT',
   'NOT_COMPATIBLE_WITH_CURRENT_EXPERIMENT','COMPATIBILITY_UNKNOWN'],
  capital_limits_increased_to_qualify_a_venue:false,
  migration_effort_levels:['LOW','MEDIUM','HIGH'],migration_implemented:false,
  credentials_requested:false,accounts_opened:false,transfers_designed:false,
  transfer_arbitrage_designed:false,is_arbitrage_claim:false,
  cross_venue_same_quote_currency_only:true,reference_data_adapter_built:false,
  gate_can_override_economic_guard:false,gate_can_override_risk_engine:false,
  gate_can_override_risk_policy:false,verdict:null,verdict_pending_frozen_experiment:true,
  economic_guard_changed:false,risk_engine_changed:false,drawdown_policy_changed:false,
  capital_limits_changed:false},
 cross_venue_research:{hypothesis:'rare tail dislocations may clear trading costs',
  reference_venues:['Binance'],reference_coverage:['BTC/MXN','ETH/MXN','SOL/MXN'],
  pairs_without_usable_reference:['XRP/MXN'],
  pairs_without_reference_reason:'Binance XRP/MXN was not trading',
  same_quote_only:true,fx_conversion_used:false,execution_venue:'Bitso',
  external_venues_are_reference_only:true,
  evidence_classes:['EXECUTABLE_BOOK','TRADE_TAPE','CANDLE_SCREENING_ONLY'],
  candle_screening_proves_executability:false,reference_spread_guard:true,
  reference_spread_artifact_is_rejected:true,
  required_executable_dislocation_bps:'160.39160000',
  threshold_derivation:'minimum_viable_gross_edge_bps(spread=0)',
  spread_double_count_avoided:true,maker_used_to_rescue_an_event:false,
  fee_used_for_threshold:'0.0078',fee_source:'ACCOUNT_CONFIRMED_SCHEDULE',
  dislocation_kinds:['RAW_MID_DISLOCATION','EXECUTABLE_BUY_DISLOCATION',
   'EXECUTABLE_SELL_DISLOCATION'],
  mechanisms:['BITSO_LAG','REFERENCE_MOVE_ONLY','BITSO_LOCAL_DISLOCATION','UNRESOLVED'],
  uses_arbitrage_terminology:false,simultaneous_hedge_exists:false,
  position_risk:'DIRECTIONAL_ONLY',predeclared_delays_seconds:[1,2,5,60],
  minimum_episode_observations:2,required_capture_hours:72,
  rare_events_are_acceptable:true,single_anomaly_cannot_be_a_candidate:true,
  development_only:true,holdout_untouched_before_freeze:true,candidate_created:false,
  candidate_fingerprint:null,validation_ran:false,capture_is_read_only:true,
  authenticated_external_calls:0,transfers_designed:false,strategy_implemented:false,
  gate_can_override_economic_guard:false,gate_can_override_risk_engine:false,
  gate_can_override_risk_policy:false,verdict:null,verdict_pending_frozen_experiment:true,
  economic_guard_changed:false,risk_engine_changed:false,drawdown_policy_changed:false,
  capital_limits_changed:false}};

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
 // Scoped to the shadow row: market codes also appear in the predeclared alpha relationships
 // table, so a page-wide match would be ambiguous rather than wrong.
 const row=document.querySelector('[data-shadow-market="BTC/MXN"]');
 expect(row).toBeTruthy();
 expect(row?.textContent).toContain('BTC/MXN');
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

test('the horizon experiment is shown as predeclared with exactly two timeframes',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Time-horizon research')).toBeTruthy();
 expect(document.querySelector('[data-horizons="15m,1h"]')).toBeTruthy();
 expect(document.querySelector('[data-horizons-fixed="true"]')).toBeTruthy();
});

test('the parameter budget is bounded and every configuration is retained',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-config-budget="4"]')).toBeTruthy();
 expect(document.querySelector('[data-all-configs-retained="true"]')).toBeTruthy();
});

test('the horizon verdict is withheld until the frozen experiment runs',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 // No verdict is published: the page must not imply a finding that has not been measured.
 const pending=document.querySelector('[data-verdict-pending="true"]');
 expect(pending).toBeTruthy();
 // Scoped to the cell: both the horizon and the asymmetry experiment withhold a verdict, so
 // a page-wide string match would be ambiguous.
 expect(pending?.textContent).toContain('PENDING FROZEN EXPERIMENT');
});

test('only taker execution may certify and the maker bound never can',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-certifying-mode="TAKER_TAKER"]')).toBeTruthy();
 expect(document.querySelector('[data-maker-may-certify="false"]')).toBeTruthy();
});

test('the four horizon profiles are shown with their timeframe and bucket size',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 const rows=document.querySelectorAll('[data-horizon-profile]');
 expect(rows.length).toBe(4);
 const hourly=Array.from(rows)
   .filter(row=>row.getAttribute('data-timeframe')==='1h');
 expect(hourly.length).toBe(2);
 expect(hourly[0].textContent).toContain('60');
});

test('no same-bar fill and no bucket filling is claimed at any horizon',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-same-bar-fills="false"]')).toBeTruthy();
 expect(document.querySelector('[data-incomplete-dropped="true"]')).toBeTruthy();
 expect(document.querySelector('[data-horizon-drawdown-changed="false"]')).toBeTruthy();
});

test('microstructure capture is shown as read-only and separate from the ledger',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Forward microstructure capture')).toBeTruthy();
 expect(document.querySelector('[data-order-capability="NONE"]')).toBeTruthy();
 expect(document.querySelector('[data-capture-mutation="NONE"]')).toBeTruthy();
 expect(document.querySelector('[data-capture-separate="true"]')).toBeTruthy();
});

test('capture provenance is REAL_CAPTURED_MICROSTRUCTURE and anomalies are not repaired',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector(
   '[data-capture-provenance="REAL_CAPTURED_MICROSTRUCTURE"]')).toBeTruthy();
 expect(document.querySelector('[data-anomalies-repaired="0"]')).toBeTruthy();
 expect(document.querySelector('[data-capture-gaps="0"]')).toBeTruthy();
});

test('queue position is reported unobservable and passive fill bounded only',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-queue-observable="false"]')).toBeTruthy();
 expect(document.querySelector('[data-passive-exactness="BOUNDED_ONLY"]')).toBeTruthy();
 // An exact fill claim would overstate evidence that public data cannot provide.
 expect(document.querySelector('[data-exact-fill-claimed="false"]')).toBeTruthy();
});

test('capture storage is bounded so a long run cannot fill the disk',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-bounded-storage="true"]')).toBeTruthy();
});

test('the asymmetry experiment is shown as predeclared, not as a result',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Asymmetric opportunity research')).toBeTruthy();
 expect(document.querySelector('[data-asym-thresholds="0.5,0.75,1.0,2.0"]')).toBeTruthy();
 expect(document.querySelector('[data-asym-thresholds-frozen="true"]')).toBeTruthy();
 // No verdict is published: the page must not imply a finding that has not been measured.
 const pending=document.querySelector('[data-asym-verdict-pending="true"]');
 expect(pending).toBeTruthy();
 // Both the horizon and the asymmetry experiment withhold their verdict, so assert on the
 // cell's own text rather than on a page-wide string match, which would be ambiguous.
 expect(pending?.textContent).toContain('PENDING FROZEN EXPERIMENT');
});

test('the asymmetry gate is shown as subordinate to both authoritative gates',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-asym-gate-research-only="true"]')).toBeTruthy();
 // Asymmetry cannot substitute for net economics, and the page must say so.
 expect(document.querySelector('[data-asym-overrides-economic="false"]')).toBeTruthy();
 expect(document.querySelector('[data-asym-overrides-risk="false"]')).toBeTruthy();
 expect(document.querySelector('[data-asym-overrides-policy="false"]')).toBeTruthy();
});

test('risk policy and the risk engine are shown as unchanged',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-asym-drawdown-changed="false"]')).toBeTruthy();
 expect(document.querySelector('[data-asym-risk-changed="false"]')).toBeTruthy();
});

test('structure is declared to use only past data and configs are all retained',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-asym-no-future-structure="true"]')).toBeTruthy();
 expect(document.querySelector('[data-asym-all-retained="true"]')).toBeTruthy();
 expect(document.querySelector('[data-asym-config-budget="4"]')).toBeTruthy();
});

test('no maker bound is computed for the asymmetry experiment',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-asym-maker-bound="false"]')).toBeTruthy();
 expect(document.querySelector('[data-asym-certifying-mode="TAKER_TAKER"]')).toBeTruthy();
});

test('each asymmetric challenger reports its concept, timeframe and risk cap',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 const rows=document.querySelectorAll('[data-asym-profile]');
 expect(rows.length).toBe(2);
 expect(rows[0].textContent).toContain('structural_invalidation_pullback');
 expect(rows[0].textContent).toContain('120');
 const hourly=Array.from(rows)
   .filter(row=>row.getAttribute('data-asym-profile')?.includes('-1h-'));
 expect(hourly.length).toBe(1);
});

test('alpha research is shown with the frozen price-only baseline and no new strategy',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Independent alpha-source research')).toBeTruthy();
 expect(document.querySelector('[data-alpha-price-only-frozen="true"]')).toBeTruthy();
 expect(document.querySelector('[data-alpha-new-strategy="false"]')).toBeTruthy();
 expect(document.querySelector('[data-alpha-new-price-only="false"]')).toBeTruthy();
 expect(document.querySelector('[data-alpha-maker-authorised="false"]')).toBeTruthy();
});

test('alpha is declared as information rather than profit',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 // The milestone asks whether information exists and separately whether it could pay. A page
 // that collapsed the two would let a real but unprofitable signal read as an edge.
 expect(document.querySelector('[data-alpha-information-not-pnl="true"]')).toBeTruthy();
});

test('the discovery parameters are predeclared within their budgets',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-alpha-declared-first="true"]')).toBeTruthy();
 const features=document.querySelector('[data-alpha-features]');
 expect(features).toBeTruthy();
 expect(features?.textContent).toContain('6 of at most 6');
 const relationships=document.querySelector('[data-alpha-relationships="4"]');
 expect(relationships).toBeTruthy();
 expect(relationships?.textContent).toContain('4 of at most 12');
 expect(document.querySelector('[data-alpha-multiplicity="true"]')).toBeTruthy();
});

test('the injected-leak control is a precondition for any verdict',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 // A measurement that cannot detect the outcome itself has no resolution; its result must be
 // unmeasurable rather than negative.
 expect(document.querySelector('[data-alpha-positive-control="true"]')).toBeTruthy();
 expect(document.querySelector('[data-alpha-blind-unmeasurable="true"]')).toBeTruthy();
 const controls=document.querySelector('[data-alpha-controls]');
 expect(controls?.textContent).toContain('FUTURE_LEAK');
 expect(controls?.textContent).toContain('SIGN_INVERSION');
});

test('the chronological holdout is preserved and was not consumed',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-alpha-holdout-preserved="true"]')).toBeTruthy();
 expect(document.querySelector('[data-alpha-holdout-consumed="false"]')).toBeTruthy();
 expect(document.querySelector('[data-alpha-development-only="true"]')).toBeTruthy();
 expect(document.querySelector('[data-alpha-validation-after-freeze="true"]')).toBeTruthy();
});

test('the alpha gate cannot override the economic guard, risk engine or policy',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-alpha-overrides-economic="false"]')).toBeTruthy();
 expect(document.querySelector('[data-alpha-overrides-risk="false"]')).toBeTruthy();
 expect(document.querySelector('[data-alpha-overrides-policy="false"]')).toBeTruthy();
 expect(document.querySelector('[data-alpha-risk-changed="false"]')).toBeTruthy();
 expect(document.querySelector('[data-alpha-drawdown-changed="false"]')).toBeTruthy();
});

test('the alpha verdict is withheld until the frozen experiment runs',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 const pending=document.querySelector('[data-alpha-verdict-pending="true"]');
 expect(pending).toBeTruthy();
 // Scoped to the cell: the horizon, asymmetry and alpha experiments all withhold a verdict, so
 // a page-wide string match would be ambiguous.
 expect(pending?.textContent).toContain('PENDING FROZEN EXPERIMENT');
});

test('every predeclared alpha feature family is shown with its interpretation',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 const rows=document.querySelectorAll('[data-alpha-feature]');
 expect(rows.length).toBe(6);
 for(const name of ['LAGGED_RETURN','MARKET_BREADTH','LEADER_FOLLOWER_DIVERGENCE'])
   expect(document.querySelector(`[data-alpha-feature="${name}"]`)).toBeTruthy();
 expect(rows[0].textContent).toContain('leader market return');
});

test('the pipeline is declared able to report an absence of alpha',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 // A discovery pipeline that always finds alpha is broken, so the ability to return a null
 // must be a property of the contract rather than a hope about the data.
 expect(document.querySelector('[data-alpha-may-report-absence="true"]')).toBeTruthy();
 expect(document.querySelector('[data-alpha-no-future-structure="true"]')).toBeTruthy();
});

test('venue economics is shown as a decision milestone that builds nothing',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Venue economics and product thesis')).toBeTruthy();
 expect(document.querySelector('[data-venue-decision="true"]')).toBeTruthy();
 expect(document.querySelector('[data-venue-builds-strategy="false"]')).toBeTruthy();
 expect(document.querySelector('[data-venue-builds-adapter="false"]')).toBeTruthy();
});

test('the frozen signal is stated with its fingerprint and is not retuned',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 // The venue arithmetic must not be readable as a re-derived signal.
 expect(document.querySelector('[data-venue-frozen-label="VALIDATED_INFORMATION_SIGNAL_V1"]'))
   .toBeTruthy();
 expect(document.querySelector('[data-venue-retuned="false"]')).toBeTruthy();
 const fingerprint=document.querySelector('[data-venue-frozen-fingerprint]');
 expect(fingerprint?.textContent).toContain('fae4faa85ef648ac');
});

test('an unknown venue rate is rendered as unknown rather than omitted',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 // Hiding the gap would make the comparison look complete when it is not.
 const unknown=document.querySelector('[data-venue-unknown-rates]');
 expect(unknown?.textContent).toContain('Coinbase Advanced');
 const coinbase=document.querySelector('[data-venue-source="Coinbase Advanced"]');
 expect(coinbase?.getAttribute('data-venue-basis')).toBe('ACCOUNT_RATE_UNKNOWN');
 expect(coinbase?.textContent).toContain('UNKNOWN');
});

test('the venue table shows the cheapest published rate and its MXN reachability',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 const rows=document.querySelectorAll('[data-venue-source]');
 expect(rows.length).toBe(4);
 const binance=document.querySelector('[data-venue-source="Binance"]');
 expect(binance?.getAttribute('data-venue-basis')).toBe('PUBLISHED_BASELINE');
 expect(binance?.textContent).toContain('0.1%');
 // Bitso is the baseline and must appear.
 expect(document.querySelector('[data-venue-source="Bitso"]')).toBeTruthy();
});

test('capital limits are unchanged and were not raised to qualify a venue',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 const capital=document.querySelector('[data-venue-capital="11"]');
 expect(capital).toBeTruthy();
 expect(capital?.textContent).toContain('50');
 expect(capital?.textContent).toContain('25');
 expect(document.querySelector('[data-venue-limits-raised="false"]')).toBeTruthy();
});

test('no credentials were requested and no account or transfer was touched',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-venue-credentials="false"]')).toBeTruthy();
 expect(document.querySelector('[data-venue-accounts="false"]')).toBeTruthy();
 expect(document.querySelector('[data-venue-transfers="false"]')).toBeTruthy();
});

test('a price difference is never presented as arbitrage',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-venue-arbitrage="false"]')).toBeTruthy();
 expect(document.querySelector('[data-venue-same-quote="true"]')).toBeTruthy();
});

test('the venue model cannot override the economic guard, risk engine or policy',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-venue-overrides-economic="false"]')).toBeTruthy();
 expect(document.querySelector('[data-venue-overrides-risk="false"]')).toBeTruthy();
 expect(document.querySelector('[data-venue-overrides-policy="false"]')).toBeTruthy();
});

test('the venue verdict is withheld until the frozen experiment runs',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 const pending=document.querySelector('[data-venue-verdict-pending="true"]');
 expect(pending).toBeTruthy();
 // Scoped to the cell: several panels withhold a verdict, so a page-wide match is ambiguous.
 expect(pending?.textContent).toContain('PENDING FROZEN EXPERIMENT');
});

test('the early-fail classification and capture ladder are visible',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector(
   '[data-venue-early-fail="STRUCTURALLY_UNTRADEABLE_FOR_THIS_ALPHA"]')).toBeTruthy();
 expect(document.querySelector('[data-venue-scenarios="100%,75%,50%,25%"]')).toBeTruthy();
 expect(document.querySelector('[data-venue-historical-friction="173.0000"]')).toBeTruthy();
});

test('cross-venue research shows Bitso as execution venue and the reference as read-only',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Cross-venue dislocation research')).toBeTruthy();
 expect(document.querySelector('[data-cv-execution-venue="Bitso"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-reference-only="true"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-same-quote="true"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-fx="false"]')).toBeTruthy();
});

test('candle screening is declared unable to prove an executable dislocation',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 // A historical-looking number must never be presented as executable evidence.
 expect(document.querySelector('[data-cv-candle-proves="false"]')).toBeTruthy();
 const evidence=document.querySelector('[data-cv-evidence]');
 expect(evidence?.textContent).toContain('CANDLE_SCREENING_ONLY');
 expect(evidence?.textContent).toContain('EXECUTABLE_BOOK');
});

test('the reference-spread guard is active and an artifact is rejected',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 // This is the guard that caught the development artefact where an apparent dislocation sat
 // inside the reference book's own bid-ask.
 expect(document.querySelector('[data-cv-spread-guard="true"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-artifact-rejected="true"]')).toBeTruthy();
});

test('the economic threshold does not charge the spread twice and never uses maker',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-cv-spread-once="true"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-maker-rescue="false"]')).toBeTruthy();
 const threshold=document.querySelector('[data-cv-threshold="160.39160000"]');
 expect(threshold?.textContent).toContain('160.3916');
});

test('a price difference is never called arbitrage and carries directional risk',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-cv-arbitrage="false"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-hedge="false"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-risk="DIRECTIONAL_ONLY"]')).toBeTruthy();
});

test('a market without a usable reference is stated rather than left blank',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 const gap=document.querySelector('[data-cv-no-reference="XRP/MXN"]');
 expect(gap).toBeTruthy();
 expect(gap?.textContent).toContain('XRP/MXN');
 // The absence must not read as evidence that XRP showed nothing.
 expect(gap?.textContent).toContain('not trading');
});

test('the capture window requirement is visible and no candidate was claimed',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-cv-required-hours="72"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-rare-ok="true"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-candidate="false"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-validation-ran="false"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-holdout="true"]')).toBeTruthy();
});

test('cross-venue research is read-only with no strategy or transfer designed',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-cv-read-only="true"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-auth-calls="0"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-transfers="false"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-strategy="false"]')).toBeTruthy();
});

test('the cross-venue model cannot override the economic guard, risk engine or policy',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(document.querySelector('[data-cv-overrides-economic="false"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-overrides-risk="false"]')).toBeTruthy();
 expect(document.querySelector('[data-cv-overrides-policy="false"]')).toBeTruthy();
});

test('the cross-venue verdict is withheld until the frozen experiment runs',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 const pending=document.querySelector('[data-cv-verdict-pending="true"]');
 expect(pending).toBeTruthy();
 expect(pending?.textContent).toContain('PENDING FROZEN EXPERIMENT');
});