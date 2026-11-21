import {useEffect, useState} from 'react';
import {Chart, DataTable} from './charts';
import {MicroLivePanel} from './MicroLivePanel';
import {RuntimePanel} from './RuntimePanel';

/**
 * The shadow / micro-live operator dashboard served by `autofund dashboard` and `autofund shadow`.
 *
 * It is read-only: there is no order, withdrawal or write control anywhere on this page, and the
 * header says so. It is still a supported surface (see docs/F4_5_DASHBOARD_SPEC.md); the MVP
 * application in MvpApp.tsx is the newer one.
 *
 * The backend payloads are loosely typed on purpose: the page only displays what the API returns.
 */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Model = Record<string, any>;

const MODELS = ['overview', 'quality', 'market', 'candles', 'equity', 'activity', 'ledger', 'sessions', 'risk', 'health', 'runtime'] as const;
const PAGES = ['Overview', 'Market', 'Activity', 'Ledger', 'Sessions', 'System'] as const;

const DISCONNECTED = 'LIVE DATA DISCONNECTED';
const CONNECTED = 'SSE CONNECTED';
const STARTING = 'SESSION STARTING';

const api = (path: string) =>
  fetch('/api/v1/' + path).then((response) => {
    if (!response.ok) throw new Error('API');
    return response.json();
  });

const isMicroLive = (runtime?: Model) => runtime?.mode === 'MICRO-LIVE' && Boolean(runtime.micro_live);

function QualityLine({quality, withBenchmark}: {quality: Model; withBenchmark?: boolean}) {
  return (
    <p className={withBenchmark ? 'warn' : undefined}>
      {quality.status} · Stale snapshots: {quality.stale_snapshots} · Out-of-order trades: {quality.out_of_order_trades} · Gaps:{' '}
      {quality.gaps}
      {withBenchmark && <> · Benchmark: {quality.benchmark_eligible ? 'ELIGIBLE' : 'NOT ELIGIBLE'}</>}
    </p>
  );
}

function Overview({data}: {data: Model}) {
  const overview = data.overview;
  const cards: [string, string][] = [
    ['Shadow Equity', overview.current_shadow_equity_mxn],
    ['Max deployment', overview.max_deployment_mxn],
    ['Net P&L', overview.net_pnl_mxn],
    ['Fees', overview.total_fees_mxn],
  ];
  return (
    <>
      <div className="cards">
        {cards.map(([title, value]) => (
          <article key={title}>
            <small>{title}</small>
            <b>{value} MXN</b>
          </article>
        ))}
      </div>
      <Chart points={data.equity} label="Shadow Equity" />
      <QualityLine quality={data.quality} withBenchmark />
    </>
  );
}

function Market({data}: {data: Model}) {
  return (
    <>
      <p>
        {data.market.market.toUpperCase()} · bid {data.market.best_bid_mxn} / ask {data.market.best_ask_mxn}
      </p>
      <Chart points={data.candles} label="Closed candlestick chart" />
      <QualityLine quality={data.quality} />
    </>
  );
}

function System({data}: {data: Model}) {
  return (
    <dl>
      <dt>Production observer</dt>
      <dd>READ ONLY</dd>
      <dt>Real execution</dt>
      <dd>DISABLED</dd>
      <dt>Write capability</dt>
      <dd>BLOCKED</dd>
      <dt>HTTP mutation endpoints</dt>
      <dd>NONE</dd>
      <dt>Accounting</dt>
      <dd>{data.health.accounting}</dd>
    </dl>
  );
}

function PageBody({page, data}: {page: string; data: Model}) {
  switch (page) {
    case 'Overview':
      return <Overview data={data} />;
    case 'Market':
      return <Market data={data} />;
    case 'Activity':
      return <DataTable rows={data.activity.items} />;
    case 'Ledger':
      return <DataTable rows={data.ledger.items} />;
    case 'Sessions':
      return (
        <>
          <DataTable rows={data.sessions.items} />
          <pre>{JSON.stringify(data.sessions.items[0], null, 2)}</pre>
        </>
      );
    case 'System':
      return <System data={data} />;
    default:
      return null;
  }
}

export function LegacyDashboard() {
  const [page, setPage] = useState<string>('Overview');
  const [data, setData] = useState<Model>({});
  const [state, setState] = useState('Loading');
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    // Read every model over REST. A missing runtime means the backend is unreachable; a micro-live
    // runtime is rendered by its own panel; a session that is still starting keeps the old data.
    const load = async () => {
      const runtime = await api('runtime').catch(() => null);
      if (!runtime) {
        setState(DISCONNECTED);
        return;
      }
      if (isMicroLive(runtime)) {
        setData({runtime});
        return;
      }
      if (runtime.status === 'STARTING' && !runtime.snapshot_ready) {
        setData((previous) => ({...previous, runtime}));
        setState(STARTING);
        return;
      }
      return Promise.all(MODELS.map(api))
        .then((values) => setData(Object.fromEntries(MODELS.map((name, i) => [name, values[i]]))))
        .catch(() => setState(DISCONNECTED));
    };

    load();
    const stream = new EventSource('/api/v1/stream');
    stream.onerror = () => {
      setConnected(false);
      setState(DISCONNECTED);
    };
    stream.addEventListener('snapshot', (event) => {
      setConnected(true);
      setState(CONNECTED);
      try {
        const payload = JSON.parse((event as MessageEvent).data);
        if (isMicroLive(payload.runtime)) {
          setData({runtime: payload.runtime});
          return;
        }
      } catch {
        // A malformed snapshot can never overwrite the data already on screen.
      }
      load();
    });
    stream.onopen = () => {
      setConnected(true);
      setState(CONNECTED);
      load();
    };
    return () => stream.close();
  }, []);

  if (isMicroLive(data.runtime)) {
    return <MicroLivePanel runtime={data.runtime} page={page} setPage={setPage} state={state} connected={connected} />;
  }
  if (!data.overview) return <main>{state}</main>;

  const overview = data.overview;
  return (
    <main>
      <header>
        {overview.demo_mode && <strong className="demo">DEMO DATA</strong>}
        <b>AutoFund</b>
        <span>MODE: SHADOW</span>
        <span>EXCHANGE: BITSO PRODUCTION</span>
        <strong>REAL TRADING DISABLED</strong>
        <strong>WRITE CAPABILITY BLOCKED</strong>
        <span>QUALITY: {overview.market_quality}</span>
      </header>
      <aside>
        {PAGES.map((name) => (
          <button key={name} onClick={() => setPage(name)} aria-current={page === name}>
            {name}
          </button>
        ))}
      </aside>
      <section>
        <small>{state}</small>
        <h1>{page}</h1>
        {data.runtime?.session_id && <RuntimePanel runtime={data.runtime} disconnected={!connected} market={page === 'Market'} />}
        <PageBody page={page} data={data} />
      </section>
    </main>
  );
}
