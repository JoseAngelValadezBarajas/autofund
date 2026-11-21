/** Small presentational pieces shared by the shadow dashboard. */

export type Row = Record<string, unknown>;

export interface Candle {
  timestamp: string;
  open: string;
  high: string;
  low: string;
  close: string;
}

export interface EquityPoint {
  equity_mxn: string;
}

const WIDTH = 550;
const LEFT = 25;

function isCandle(point: Candle | EquityPoint): point is Candle {
  return (point as Candle).open !== undefined;
}

/** A plain table of whatever rows the backend returned, one column per key of the first row. */
export function DataTable({rows}: {rows: Row[]}) {
  if (!rows.length) return <p>No activity recorded</p>;
  return (
    <div className="table">
      <table>
        <thead>
          <tr>{Object.keys(rows[0]).map((key) => <th key={key}>{key}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {Object.values(row).map((value, j) => (
                <td key={j}>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Candlesticks when the points carry OHLC, otherwise an equity line. */
export function Chart({points, label}: {points: (Candle | EquityPoint)[]; label: string}) {
  if (!points.length) {
    return <p>{label === 'Shadow Equity' ? 'No equity history available' : 'No closed candles yet'}</p>;
  }
  const candles = isCandle(points[0]);
  const values = points.flatMap((p) => (isCandle(p) ? [Number(p.low), Number(p.high)] : [Number((p as EquityPoint).equity_mxn)]));
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high - low || 1;
  const y = (value: string | number) => 150 - ((Number(value) - low) / span) * 110;
  const x = (index: number) => LEFT + index * (WIDTH / Math.max(points.length - 1, 1));

  return (
    <figure>
      <figcaption>{label}</figcaption>
      <svg aria-label={label} className="chart" viewBox="0 0 600 180">
        {candles ? (
          (points as Candle[]).map((p, i) => {
            const color = Number(p.close) >= Number(p.open) ? '#8ee3c1' : '#ffbf69';
            return (
              <g key={p.timestamp}>
                <line x1={x(i)} x2={x(i)} y1={y(p.high)} y2={y(p.low)} stroke={color} strokeWidth="2" />
                <rect
                  x={x(i) - 7}
                  y={Math.min(y(p.open), y(p.close))}
                  width="14"
                  height={Math.max(2, Math.abs(y(p.open) - y(p.close)))}
                  fill={color}
                />
              </g>
            );
          })
        ) : (
          <polyline
            fill="none"
            stroke="#67d5ff"
            strokeWidth="3"
            points={(points as EquityPoint[]).map((p, i) => `${x(i)},${y(p.equity_mxn)}`).join(' ')}
          />
        )}
      </svg>
    </figure>
  );
}
