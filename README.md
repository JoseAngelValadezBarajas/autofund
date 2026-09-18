# AutoFund 0.1 ? F0 Financial Core

**F0 IS NOT A PROFITABLE TRADING SYSTEM.**

N?cleo financiero y de riesgo en Python 3.12+, exclusivamente paper y en MXN.
Sin conexiones externas, dinero real, estrategias, retiros ni credenciales.
No contiene exchanges, servicios web, bases de datos ni infraestructura cloud.

## Instalaci?n y ejecuci?n

Desde este directorio, en PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
.venv\Scripts\python examples/f0_demo.py
```

En Linux/macOS sustituir `.venv\Scripts\python` por `.venv/bin/python`.
Dependencias de runtime: ninguna. pytest s?lo es una dependencia de desarrollo.

## Uso

```python
from decimal import Decimal as D
from autofund import Wallet, CapitalManager, PaperExecutionEngine

wallet = Wallet()
wallet.deposit(D("50"))
capital = CapitalManager()
assert wallet.equity({}) == D("50")
assert capital.available_for_new_buys(wallet, {}) == D("25")
engine = PaperExecutionEngine(wallet, capital, fee_rate=D("0.001"))
buy = engine.buy("BTC/MXN", D("10"), D("1000000"))
sell = engine.sell("BTC/MXN", buy.quantity, D("1040000"))
wallet.assert_invariants()
print(wallet.equity({}), wallet.realized_pnl())
```

## Arquitectura

Solicitud futura ? c?lculo de CapitalManager ? validaci?n de RiskEngine ?
PaperExecutionEngine ? aplicaci?n privada de Wallet + Ledger.

`buy`/`sell` son la fachada de solicitudes: internamente validan riesgo antes de
aplicar cualquier fill. Una estrategia futura recibir? esta fachada, nunca la
wallet. CapitalManager calcula l?mites y RiskEngine s?lo lee contabilidad.
El motor no expone la wallet p?blicamente; el propietario de la aplicaci?n
puede depositar fondos ficticios. Las propiedades contables son de s?lo lectura,
las posiciones/fills/entradas son dataclasses congeladas y el ledger es una tupla.
Los atributos privados de Python no son un aislamiento frente a c?digo hostil.

Cada operaci?n prepara y reconcilia el estado candidato antes de publicarlo.
El ledger reconstruye cash, cantidades, costo y P&L; no se reparan diferencias.
`assert_invariants()` lanza `AccountingInvariantError` ante corrupci?n.

Para compras con otras posiciones, proporcionar `marks={"ETH/MXN": D("...")}`.
El precio de referencia de la orden es la marca de su propio mercado. Toda
posici?n abierta necesita una marca positiva en MXN; no se usan precios obsoletos
ni se valora una posici?n faltante en cero. Las ventas pueden reducir riesgo sin
necesitar marcas de otros activos.

## Decisiones y l?mites

- Decimal obligatorio: floats, ints, NaN e infinitos se rechazan.
- Precisi?n local de 50 d?gitos; residual de compra expl?cito en fill y ledger.
- El presupuesto incluye todo el fee; costo promedio para ventas parciales.
- El l?mite de capital usa equity marcado actual, no un importe fijo.
- La reserva es un objetivo al admitir compras, no un rebalanceo autom?tico.
- Estado en memoria, s?ncrono, un ?nico escritor; sin persistencia ni concurrencia.
- Sin lotes, ticks, redondeo a centavos, liquidaci?n, impuestos ni adapters.

La especificaci?n congelada y los criterios de aceptaci?n est?n en
[docs/F0_SPEC.md](docs/F0_SPEC.md). La siguiente fase propuesta es F1:
replay hist?rico determinista, conservando este n?cleo independiente del mercado.

Las pruebas parametrizadas siguen la [documentaci?n oficial de pytest](https://docs.pytest.org/en/stable/how-to/parametrize.html).
