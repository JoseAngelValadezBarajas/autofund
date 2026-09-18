# AutoFund 0.1 ? F0 specification

Estado: contrato F0. F0 IS NOT A PROFITABLE TRADING SYSTEM.

## Scope

Contabilidad paper inmediata, MXN, posiciones largas, dep?sitos, costo promedio,
gesti?n de capital, riesgo y auditor?a. Python 3.12+, runtime sin dependencias.
Sin exchanges/adapters, red, secretos, retiros, estrategias, datos hist?ricos,
replay, APIs web, frontend, bases de datos, margen, shorts ni deployment.
No se introduce un Protocol todav?a: s?lo existe un motor; futuros adapters deben
producir fills mediante una frontera de ejecuci?n que conserve este contrato.

## Financial invariants

1. Cash = suma ordenada de cash_delta_mxn en el ledger bajo la pol?tica decimal.
2. Equity = cash + suma(quantity * mark); deployed_value es esa suma de posiciones.
3. Cash, cantidades y costos nunca negativos; posici?n cerrada implica costo cero.
4. Ledger reconstruye por mercado quantity, cost_basis y realized_pnl acumulado.
5. Cada venta retira costo promedio, no m?s activos que los disponibles.
6. Entradas consecutivas, dep?sitos positivos y signo de movimientos consistente.
7. Ninguna operaci?n se publica si su estado candidato no reconcilia.

Toda inconsistencia del estado se convierte en AccountingInvariantError. No hay
reparaci?n autom?tica ni tolerancias en comparaciones de reconciliaci?n.
Marcas omitidas/incorrectas son InvalidFinancialInput, no corrupci?n contable.

## Decimal policy

Todos los l?mites financieros requieren instancias Decimal finitas; tampoco se
coercionan ints ni strings. Inputs con m?s de 50 d?gitos significativos o magnitud
no nula fuera de exponentes ajustados [-100,100] se rechazan. Cada operaci?n usa
un contexto nuevo, precisi?n 50 y ROUND_HALF_EVEN; no hereda precisi?n, traps ni
redondeo del llamador y no cambia su contexto. No se cuantiza a centavos ni ticks.

Una divisi?n peri?dica no permite cumplir simult?neamente todas las identidades
racionales con Decimal finito. Se prioriza presupuesto exacto y trazabilidad:
BUY calcula quantity, gross y fee nominal con ROUND_DOWN a 50 d?gitos; fee efectivo
= budget - gross. rounding_adjustment_mxn = fee efectivo - fee nominal se guarda
en Fill y LedgerEntry. Este residual num?rico se incluye en fee y costo; no es
un fee adicional de un exchange. Con fee configurado cero puede existir un
residual diminuto. gross + fee efectivo = budget bajo este contexto.
SELL calcula gross y fee a precisi?n 50 HALF_EVEN, sin ajuste adicional.
Las sumas/valoraciones/costos se efect?an ordenadamente bajo el mismo contexto;
para recomputar externamente cifras de 50 d?gitos, usar localcontext(prec=50).
No se afirma aritm?tica racional infinita ni precisi?n monetaria ilimitada.
Un movimiento no nulo que desaparezca por completo al sumarse al cash o a la
cantidad existente se rechaza antes de publicar, con InvalidFinancialInput.

## Ledger policy

LedgerEntry congelada: entry_id secuencial determinista, type DEPOSIT/BUY/SELL,
cash_delta_mxn, market, asset_delta, fee_mxn, realized_pnl_mxn, note,
cost_basis_delta_mxn y rounding_adjustment_mxn. No timestamps no deterministas.
Tupla p?blica append-only; copiar vistas no concede permisos de escritura.
Cash y posiciones se preparan y verifican antes de reemplazar el estado.
F0 no promete persistencia, hashes antimanipulaci?n ni seguridad frente a c?digo
que viole deliberadamente los atributos privados. Manipular estado inconsistente
se detecta; reescribir coherentemente todo el ledger no es detectable sin un
ancla externa. Modelo de operaci?n s?ncrono de un ?nico escritor.

## Position accounting

BUY: quantity += fill.quantity; cost_basis += budget (incluye fee efectivo).
SELL parcial: removed = old_cost * (sold_quantity / old_quantity).
SELL total: removed = old_cost exactamente, eliminando residuos de costo.
Net = gross - fee; realized = net - removed. P&L se acumula por posici?n y ledger.
Se conserva la posici?n con quantity=0 y cost=0 para conservar su P&L hist?rico.
Recomprar comienza con costo nuevo y conserva el P&L acumulado.

## Capital manager

Default fraction = Decimal("0.50"), configurable en [0,1].
Limit = equity * fraction.
Available = min(cash, max(0, limit - deployed_value)).
No importes cacheados. Si equity pasa de 50 a 52, limit pasa de 25 a 26.
La reserva objetivo inicial es 50%; no es una bolsa separada ni una garant?a
permanente despu?s de movimientos de precios. Si hay sobreexposici?n por mercado,
se bloquean compras y se permiten ventas; no se liquida autom?ticamente.

## Risk rules and responsibility boundaries

CapitalManager s?lo calcula; RiskEngine s?lo valida. Execution es el ?nico
componente que llama a Wallet._apply_fill. La aplicaci?n deposita y consulta;
una estrategia futura recibe ?nicamente la fachada de ?rdenes, no Wallet.
Cada buy/sell valida contabilidad y riesgo antes de mutar; no hay API p?blica
para aplicar fills arbitrarios. No hay ?rdenes pendientes ni reservas concurrentes.

BUY: mercado BASE/MXN, budget>0, referencia>0, budget>=minimum_order (default 1
MXN), budget<=cash y budget<=available. Todas las posiciones abiertas necesitan
marcas positivas. Referencia de la orden sustituye la marca de su propio mercado.
SELL: referencia>0, quantity>0, posici?n existente y quantity<=owned.
El m?nimo se aplica a BUY, no a SELL, para permitir cerrar residuos.
Riesgo rechazado lanza RiskRejected; falta de cash usa InsufficientFunds, que
hereda de RiskRejected. Tipos/NaN/configuraci?n/mercados inv?lidos usan
InvalidFinancialInput. Configuraci?n: 0<=fee_rate<1; 0<=slippage_bps<10000.

## Paper fills

BUY price = reference * (1 + slippage_bps/10000).
Quantity = budget / (price * (1+fee_rate)). Gross = quantity * price.
Fee efectivo y residual seg?n Decimal policy; cash_delta = -budget.
SELL price = reference * (1 - slippage_bps/10000).
Gross = quantity * price; fee = gross * fee_rate; cash_delta = gross-fee.
Fill inmutable contiene side, mercado, quantity, price, gross, fee, cash_delta,
realized_pnl y residual. Sin ?rdenes pendientes ni latencia simulada.

## Acceptance criteria

La suite pytest verifica expl?citamente: dep?sito 50, equity 50, disponible 25,
BUY 25 aceptado y >25 rechazado, m?nimo, fees incluidos, consumo exacto del
presupuesto, proceeds netos, round-trips ganadores/perdedores, oversell, importes
y precios no positivos, floats, reconciliaci?n y corrupci?n manual, prohibici?n
de negativos, cierre a costo cero, rec?lculo din?mico y slippage adverso en ambos
sentidos. Adem?s: costo promedio parcial, m?ltiples mercados, vistas inmutables,
NaN/inf, configuraci?n inv?lida, atomicidad de rechazos, secuencia de 100 ciclos
con semilla fija e independencia del contexto Decimal ambiente.
Escenario obligatorio: 50 iniciales, compra BTC/MXN budget 10 a 1,000,000, venta
total a 1,040,000; fees 0.001, cash final >50, costo y cantidad finales cero.
Todos los tests deben pasar y la demo debe reconciliar antes de declarar PASS.

## F1 boundary

F1 ? deterministic historical market replay. A?adir reloj/eventos/datos de
replay deterministas, conservando Wallet/Ledger como autoridad. No conectar
exchanges ni dinero real como parte de F0. Tick/lot sizes, persistencia,
concurrencia, FX y adapters requieren especificaci?n posterior expl?cita.
