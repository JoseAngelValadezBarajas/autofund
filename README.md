# AutoFund 0.2 ? Financial Core + Deterministic Replay

**F0 IS NOT A PROFITABLE TRADING SYSTEM.**
**F1 IS NOT A PROFITABLE TRADING SYSTEM.**

AutoFund contiene un n?cleo financiero paper en MXN y un replay hist?rico local,
determinista y auditable. Python 3.12+, sin dependencias de runtime.
No conecta exchanges ni usa dinero real; tampoco descarga datos. La ?nica estrategia
es un dummy deliberadamente sencillo; no hay b?squeda de rentabilidad.

F0 est? congelado en `066a54c`: su c?digo, pruebas, especificaci?n y demo no se
modifican para F1. El nuevo paquete `autofund.replay` utiliza Wallet,
CapitalManager, RiskEngine y PaperExecutionEngine mediante sus contratos.

## Instalaci?n y pruebas

Desde `C:\Dev\Negocios\autofund`, en PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
.venv\Scripts\python examples/f0_demo.py
.venv\Scripts\python examples/f1_demo.py
.venv\Scripts\python examples/f1_demo.py --output artifacts/f1_demo.json
```

En Linux/macOS usar `.venv/bin/python`. pytest es una dependencia de desarrollo.
El JSON es opcional y contiene datos, par?metros, se?ales, resultados de
intenciones, fills, ledger, posiciones, curva de equity, m?tricas y fingerprints.
`artifacts/` es local y no se versiona. No se necesitan claves ni servicios.

## Ejecutar F1 desde Python

```python
from decimal import Decimal
from autofund.replay import (
    ReplayConfig, ReplayRunner, SimpleMeanReversionV0, load_csv,
)

dataset = load_csv("examples/data/mean_reversion_market.csv", market="BTC/MXN")
config = ReplayConfig(
    initial_equity=Decimal("50"),
    fee_rate=Decimal("0.01"),
    slippage_bps=Decimal("100"),
)
strategy = SimpleMeanReversionV0(allocation_fraction=Decimal("0.20402"))
result = ReplayRunner(config).run(dataset=dataset, strategy=strategy)
assert result.metrics.final_equity_mxn == Decimal("49.6")
assert result == ReplayRunner(config).run(dataset=dataset, strategy=strategy)
print(result.result_fingerprint)
```

Este ejemplo pierde exactamente 0.40 MXN: 0.20 de fees y 0.20 de slippage.
El resultado no se ha ajustado para producir una ganancia. El golden test congela
este c?lculo manual y su fingerprint completo.

## Arquitectura y decisiones

CSV validado ? ReplayClock ? ejecuci?n de intenci?n pendiente en la apertura ?
marcado de equity ? snapshot de candle cerrado ? Strategy ? OrderIntent.
La siguiente apertura vuelve a comprobar capital/riesgo en F0 antes del fill.

- La estrategia recibe ?nicamente valores inmutables y candles ya observados.
  Nunca recibe Wallet, Ledger, el dataset completo ni un ejecutor.
- Los timestamps son etiquetas UTC de candles, con fases OPEN y CLOSE ordenadas;
  no se inventa una duraci?n intrabar. Una se?al de N ejecuta como pronto en N+1.
- Al finalizar se cancelan intenciones y se marcan posiciones al ?ltimo close;
  no se fuerza liquidaci?n. El resultado separa P&L realizado/no realizado.
- Decimal obligatorio. Se conserva la pol?tica de precisi?n/residuales de F0.
  CSV inv?lido se rechaza; no se repara ni ordena silenciosamente.
- Hashes SHA-256 de JSON can?nico. Rutas, metadatos y tiempo real no intervienen.
  run_id identifica los inputs; result_fingerprint identifica el resultado.
- Trade cerrado significa episodio flat-to-flat; se cuenta aparte cada fill.
  Cost-addback es una m?trica expl?cita, no un backtest ficticio sin costos.
- El l?mite controla admisi?n de compras. Un movimiento de mercado puede elevar
  despu?s la exposici?n; la m?trica m?xima lo muestra y no rebalancea en secreto.

## Documentaci?n, l?mites y validaci?n

[F0_SPEC](docs/F0_SPEC.md) conserva los contratos financieros.
[F1_SPEC](docs/F1_SPEC.md) congela schema, temporalidad, aislamiento, ciclo,
serializaci?n, f?rmulas, l?mites y golden replay. Fixtures peque?os est?n en
`tests/fixtures/`; pruebas F1 en `tests/replay/`.

F1 soporta un mercado BASE/MXN por run, un escritor, fills paper completos e
inmediatos en la siguiente apertura y datos en memoria. No simula ticks, spreads,
liquidez, latencia, ejecuci?n intrabar ni exchange constraints. Drawdown utiliza
aperturas posteriores a ejecuci?n y cierres, no una trayectoria inventada entre
high/low. La API a?sla estrategias normales, no es un sandbox de Python hostil.

Las pruebas comprueban igualdad exacta entre runs, procesos, contextos Decimal,
rutas y timezones. No utilizan tolerancias para demostrar reproducibilidad.
El baseline financiero sigue cubierto por sus 76 pruebas originales.

Herramientas opcionales de revisi?n (no runtime):

```powershell
.venv\Scripts\python -m pip install ruff mypy
.venv\Scripts\python -m ruff check src tests examples
.venv\Scripts\python -m ruff format --check src tests examples
.venv\Scripts\python -m mypy --strict --follow-imports=silent src/autofund/replay
```
