/**
 * Strategy Research: profile x market evidence, with honest lifecycle labelling.
 *
 * Four lifecycle states are shown, and they are deliberately distinct because
 * collapsing them would be a lie of omission:
 *
 *   RESEARCH                evaluated, no certified market
 *   SHADOW                  evaluated against real or captured data
 *   PRODUCTION CERTIFIABLE  passed every evidence requirement
 *   ACTIVE PRODUCTION       the Champion, the only profile trading real money
 *
 * The panel is informational. It exposes no BUY/SELL control, and
 * `multi_market_production` is displayed as DISABLED so the current boundary is
 * visible rather than implied.
 */

export interface ResearchProfile {
  profile_id:string; strategy_id:string; version:string;
  parameters:Record<string,string>;
  target_model:{version:string;atr_multiple:string;floor_bps:string;cap_bps:string};
  fingerprint:string; strategy_fingerprint:string; profile_contract_version:string;
  markets:string[]; evaluator:string; description:string;
  markets_certified:string[]; lifecycle:string;
}

export interface StrategyResearchView {
  research_version:string;
  promotion:string;
  multi_market_production:string;
  champion:{profile_id:string;strategy_id:string;version:string;fingerprint:string;
    certification_status:string;lifecycle:string;intended_gross_edge_bps:string};
  profiles:ResearchProfile[];
  economic_viability:{taker_fee_rate:string;spread_bps:string;account_fee_confirmed:boolean;
    minimum_viable_gross_edge_bps:string|null;champion_intended_gross_edge_bps:string;
    champion_economically_viable:boolean|null};
  markets:{market:string;status:string;strategy_compatibility:string;market_class:string;
    lifecycle:string}[];
  shadow:Record<string,unknown>[];
  profile_by_id:string[];
}

/** Shadow rows carry whichever fields the pipeline produced; read them defensively. */
type ShadowRow = Record<string, unknown>;

const text=(value:unknown,fallback='—')=>
  value===null||value===undefined||value===''?fallback:String(value);

const LIFECYCLE_LABEL:Record<string,string>={
  RESEARCH:'RESEARCH', SHADOW:'SHADOW', PRODUCTION_CERTIFIABLE:'PRODUCTION CERTIFIABLE',
  ACTIVE_PRODUCTION:'ACTIVE PRODUCTION', RESEARCH_ONLY:'RESEARCH ONLY',
};

/**
 * Economic viability of the active Champion. This is the 0.1.3 finding made
 * visible: a 20 bps target cannot pay a ~156 bps round trip.
 */
function ChampionViability({research}:{research:StrategyResearchView}){
  const viability=research.economic_viability;
  const viable=viability.champion_economically_viable;
  return <>
    <h3>Champion economic viability</h3>
    <p className="warn">
      {viable===null
        ? 'Account fee not confirmed yet: viability cannot be established.'
        : viable
          ? 'The Champion\u2019s intended move clears estimated friction.'
          : 'The Champion\u2019s intended move is below estimated friction, so its BUY signals are refused by the economic guard.'}
    </p>
    <div className="table"><table><tbody>
      <tr><th>Confirmed taker fee</th><td>{viability.account_fee_confirmed?viability.taker_fee_rate:'UNAVAILABLE'}</td></tr>
      <tr><th>Observed spread (bps)</th><td>{text(viability.spread_bps)}</td></tr>
      <tr><th>Champion intended gross edge (bps)</th><td>{viability.champion_intended_gross_edge_bps}</td></tr>
      <tr><th>Minimum viable gross edge (bps)</th><td>{text(viability.minimum_viable_gross_edge_bps)}</td></tr>
      <tr><th>Champion economically viable</th>
        <td className={viable?'positive':'negative'}>{viable===null?'UNKNOWN':viable?'YES':'NO'}</td></tr>
    </tbody></table></div>
  </>;
}

function ProfileTable({research}:{research:StrategyResearchView}){
  return <>
    <h3>Strategy profiles</h3>
    <div className="table"><table>
      <thead><tr><th>Profile</th><th>Strategy</th><th>Ver.</th><th>Lifecycle</th>
        <th>Markets certified</th><th>ATR multiple</th><th>Target floor</th><th>Target cap</th>
        <th>Fingerprint</th></tr></thead>
      <tbody>{research.profiles.map(profile=><tr key={profile.profile_id}
        data-profile={profile.profile_id} data-lifecycle={profile.lifecycle}>
        <td>{profile.profile_id}</td><td>{profile.strategy_id}</td><td>{profile.version}</td>
        <td><span className="status" data-lifecycle={profile.lifecycle}>
          {profile.lifecycle==='ACTIVE_PRODUCTION'?LIFECYCLE_LABEL.ACTIVE_PRODUCTION:'RESEARCH'}</span></td>
        <td>{profile.markets_certified.length?profile.markets_certified.join(', '):'NONE'}</td>
        <td>{profile.target_model.atr_multiple}</td>
        <td>{profile.target_model.floor_bps} bps</td>
        <td>{profile.target_model.cap_bps} bps</td>
        <td><code title={profile.fingerprint}>{profile.fingerprint.slice(0,16)}…</code></td>
      </tr>)}</tbody></table></div>
  </>;
}

function ShadowTable({rows}:{rows:ShadowRow[]}){
  if(!rows.length)return <p>No shadow evaluation recorded yet. Shadow research evaluates
    discovered markets that have captured candles and an observed order book; a market
    without them is excluded rather than given synthetic data.</p>;
  return <>
    <h3>Shadow research per market</h3>
    <div className="table"><table>
      <thead><tr><th>Market</th><th>Profiles</th><th>Evaluations</th><th>Signals</th>
        <th>Shadow trades</th><th>Gross P&amp;L</th><th>Fees / friction</th><th>Net P&amp;L</th>
        <th>Drawdown</th><th>Economic reject rate</th><th>Data source</th><th>Evidence</th></tr></thead>
      <tbody>{rows.map((row,index)=><tr key={String(row.market??index)}>
        <td>{text(row.market)}</td>
        <td>{(row.profiles as unknown[]|undefined)?.length??0}</td>
        <td>{text(row.evaluations, '0')}</td>
        <td>{text(row.signals, '0')}</td>
        <td>{text(row.shadow_trades, '0')}</td>
        <td>{text(row.gross_pnl_mxn, '0')}</td>
        <td>{text(row.fees_mxn, '0')}</td>
        <td>{text(row.net_pnl_mxn, '0')}</td>
        <td>{text(row.max_drawdown_mxn, '0')}</td>
        <td>{text(row.economic_reject_rate, '0')}</td>
        <td>{text(row.data_source)}</td>
        <td><span className="status" data-evidence={String(row.evaluated)}>
          {row.evaluated?'EVALUATED':'AWAITING DATA'}</span></td>
      </tr>)}</tbody></table></div>
  </>;
}

function MarketTable({research}:{research:StrategyResearchView}){
  if(!research.markets.length)return null;
  return <>
    <h3>Market x profile compatibility</h3>
    <p>Compatibility is declared per profile. A BTC profile is not assumed to be an ETH or
      XRP profile, and stablecoin/fiat-like books are excluded from volatile-crypto research.</p>
    <div className="table"><table>
      <thead><tr><th>Market</th><th>Class</th><th>Discovery status</th>
        <th>Strategy compatibility</th><th>Lifecycle</th></tr></thead>
      <tbody>{research.markets.map(market=><tr key={market.market}>
        <td>{market.market}</td><td>{market.market_class}</td><td>{market.status}</td>
        <td>{market.strategy_compatibility}</td>
        <td>{LIFECYCLE_LABEL[market.lifecycle]??market.lifecycle}</td>
      </tr>)}</tbody></table></div>
  </>;
}

export function StrategyResearchPage({research}:
  {research:StrategyResearchView|undefined|null}){
  if(!research)return <article>
    <h2>Strategy research</h2>
    <p className="warn">No strategy research evidence published yet.</p>
    <p>Profiles are versioned and fingerprinted. The economic guard remains the sole authority
      on whether a proposed trade is financially admissible; no economically viable
      opportunity means no trade.</p></article>;
  return <article className="strategy-research">
    <h2>Strategy research</h2>
    <div className="real-money" role="status">
      <strong>RESEARCH ONLY</strong>
      <p>Promotion {research.promotion} · Multi-market Production
        {' '}{research.multi_market_production} · Production remains BTC/MXN</p>
    </div>
    <p>Each profile proposes an opportunity and states the gross price move it intends to
      capture. <b>EconomicEdgeGuard</b> independently decides whether that proposal is
      admissible after real fees, spread and depth-walked slippage. A profile whose intended
      move cannot clear friction reports NOT_VIABLE. No economically viable opportunity means
      no trade, and nothing here is promoted automatically.</p>

    <h3>Champion</h3>
    <div className="table"><table><tbody>
      <tr><th>Profile</th><td>{research.champion.profile_id}</td></tr>
      <tr><th>Version</th><td>{research.champion.version}</td></tr>
      <tr><th>Certification</th><td>{research.champion.certification_status}</td></tr>
      <tr><th>Lifecycle</th><td>{LIFECYCLE_LABEL.ACTIVE_PRODUCTION}</td></tr>
      <tr><th>Fingerprint</th><td><code>{research.champion.fingerprint}</code></td></tr>
    </tbody></table></div>

    <ChampionViability research={research}/>
    <ProfileTable research={research}/>
    <ShadowTable rows={research.shadow}/>
    <MarketTable research={research}/>

    <p className="policy">Research contract {research.research_version} · Portfolio-level
      selection contract prepared for MVP 0.2 (not activated).</p>
  </article>;
}
