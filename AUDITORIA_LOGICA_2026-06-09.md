# Auditoría completa de lógica — Celerity Bot
*2026-06-09. Código + datos reales (143 trades spot, 43 futures, logs, estado de los agentes), evaluado contra los principios de `knowledge/` (Douglas, Steenbarger, Hougaard, Murphy, Elder, Wyckoff).*

---

## 1. Los números (verificados hoy)

| Métrica | Spot (2-may → 9-jun) | Futures/Agentes (paper) |
|---|---|---|
| Trades cerrados | 143 | 43 |
| PnL neto | **−$25.68** | −$1.08 |
| Win rate | **32.2%** | 35% |
| Ganancia media | +$0.38 | +$0.17 |
| Pérdida media | **−$0.45** | −$0.13 |
| Fees pagados | $9.51 (37% de la pérdida) | — |
| Expectativa/trade | **−$0.18** | −$0.03 |

Regla de Hougaard (`knowledge/03`): con 32% de aciertos necesitás ganar ≥2.1× lo que perdés por trade. Hoy la relación es **0.84×** — el sistema corta ganancias y deja correr… nada, porque casi nunca llega al TP (solo 2 TAKE_PROFIT en 143 trades). El sangrado principal: **26 stop-losses = −$23.80**, prácticamente toda la pérdida.

---

## 2. Hallazgos críticos (por qué pierde)

### H1 — Sistema long-only en mercado bajista (causa raíz estructural)
El bot spot **solo puede comprar**. Los logs de hoy muestran BTC/DOT/PAXG en régimen TRENDING **down** / VOLATILE down. Murphy (`knowledge/04`): nunca operar contra la tendencia del timeframe superior. Elder (`knowledge/05`): la "marea" manda. Un sistema que solo abre longs mientras la marea es bajista no tiene edge posible — los 15 filtros que se le agregaron solo reducen la frecuencia de la pérdida, no la revierten. Pierde incluso en bruto (−$16 sin fees).

### H2 — Asimetría invertida: el diseño garantiza que el SL llegue antes que el TP
- `check_stop_loss_take_profit` resta el `cost_drag` (~0.25%) del PnL: el SL salta **antes** (en −1.95% bruto para SL 2.2%) y el TP se aleja (necesita +4.65% bruto para TP 4.4%). El doble castigo está comentado como corregido en el threshold pero sigue vivo en SL/TP.
- En velas de 5m, un movimiento de +4.65% antes de −1.95% es estadísticamente raro → resultado real: 26 SL vs 2 TP.
- Las salidas "AI Signal" (34 trades, −$6.19) exigen 3 ciclos + score < −0.30 + 45 min de hold: para cuando se cumplen, la pérdida ya creció. Es exactamente el patrón "el perdedor promedio" que describe Hougaard.

### H3 — risk_level fue subido de 2 → 5 (9-jun 02:06)
`data/risk_level.json` = 5, modificado vía dashboard (el optimizer no lo toca). Esto reactivó `trade_volatile=True`, umbral 0.23, sizing 7% del capital — deshaciendo el modo ultra-conservador del plan del 6-jun **mientras la expectativa sigue negativa**. Douglas (`knowledge/01`): subir agresividad para "recuperar" es el error clásico; el sizing no debe depender del resultado reciente.

### H4 — La capa de noticias está efectivamente muerta (tu queja es correcta)
En `sentiment.py`:
- Para cualquier par que no sea BTC o PAXG, los términos de búsqueda son el símbolo literal: `"solusdc"`, `"xrpusdc"`, `"linkusdc"`. **Ningún titular contiene eso** → `news_score = 0` para 7 de 9 pares, siempre.
- Lo que queda es Fear&Greed global (igual para todos los pares, se actualiza 1 vez/día) + cambio de precio 24h (eso no es "noticias", es momentum rezagado).
- Peso total del sentiment: 12-15% → aun cuando funciona, casi no mueve la decisión.
- No hay calendario de eventos macro (FOMC, CPI, vencimientos) ni detección de noticias de alto impacto que pausen el trading.

### H5 — La capa Claude del bot spot está muerta desde el 4 de mayo
`claude_agent.py`: `ACTIVATION_THRESHOLD = 0.55`, pero el score combinado casi nunca supera ~0.35 (el umbral de compra es 0.23). Resultado verificado en logs: **2,582 análisis de Claude hasta el 4-may, cero desde entonces**. Pagaste un mes de operación sin la capa de razonamiento que creés que está filtrando trades.

### H6 — Kill switch de futures atascado: los agentes no operan desde el 5-jun
- Último trade de futures: 5-jun. Hoy el log repite `KILL SWITCH — daily loss limit reached` cada 30 segundos.
- Bug: el reset diario de `realized_pnl_today` solo ocurre **dentro de `open_position()`** (futures_trader.py ~línea 280), pero el chequeo del orchestrator (`_risk_checks_pass`) bloquea **antes** de llegar ahí → un kill switch disparado **nunca se rearma al día siguiente**. Los agentes llevan 4 días congelados y nadie lo notó.

### H7 — Sobreajuste a ruido: filtros calibrados con 5-10 trades
Violación directa de Steenbarger (`knowledge/02`: "no optimizar sobre ruido") y Douglas (evaluar por series de ≥25):
- `DEAD_HOURS_UTC` bloquea **12 de 24 horas** del día, derivado de "4-5 trades por ventana con 0% WR". Con n=4 eso es ruido puro.
- Los comentarios del código documentan al optimizer ajustando `RSI_BUY_MAX`, `MIN_TECH_SCORE`, `VOL_MIN` con muestras de 7-18 trades, cada 48h.
- Hay ~15 filtros de entrada apilados (hora muerta, circuit breaker, breadth, cooldown×2, max posiciones, correlación BTC×4, volumen, RSI, adaptive, technical, extensión de precio, anti-chop, cost gate). Nadie sabe cuál aporta y cuál estorba; juntos son una estrategia distinta de la que se diseñó, nacida de parches reactivos.

### H8 — Un solo timeframe (5m). Sin estructura
Todo se decide en velas de 5m. No existe la Pantalla 1 de Elder (tendencia en 1h/4h/1d) ni lectura estructural Wyckoff (rangos, springs, esfuerzo/resultado). El "régimen" del Adaptive layer se calcula sobre las mismas velas de 5m → detecta el chop después de comprado.

---

## 3. ¿Están aprendiendo los dos agentes? Respuesta corta: **no, y peor: se sabotean entre sí**

| Componente | ¿Funciona? | Evidencia |
|---|---|---|
| OnlineML (perceptrón por agente) | Actualiza pesos, pero es **anti-predictivo** | MomentumHunter: 27 samples, accuracy **37%**. ReversalSniper: 10 samples, accuracy **30%**. Peor que una moneda — su score (peso 25%) mete ruido. Hubo un reset manual el 19-may (`NUCLEAR_BACKUP`). |
| Tournament (evolución genética) | **No evoluciona** | 86 eventos: 69 `defensive_mode`, 16 `skipped`, **1 solo crossover** en 5 semanas. |
| Defensive mode | Activo, y es un **círculo vicioso** | Cada 6h, al ver Sharpe<0, fuerza `min_confidence=0.70` y **recorta el SL a ≤1.2%**. SL más chico en mercado volátil → más stop-outs → Sharpe peor → más defensive mode. El sistema "aprende" a perder más seguido. |
| Reflection (Claude post-trade) | Generó 2 reflexiones, **0 aplicadas** | Ambas sugieren **agrandar** el SL ("SL 1.1% con volatilidad 0.97% es muy chico" — correcto según ATR). Necesita 3 sugerencias consecutivas para aplicar… y aunque aplicara, defensive_mode lo pisa 6h después. **Reflection dice "SL más grande", defensive mode fuerza "SL más chico": los dos subsistemas de aprendizaje tiran en direcciones opuestas.** |
| Adjudicator | Funciona | Resuelve conflictos LONG vs SHORT en logs de hoy. |
| Aprendizaje, en general | Congelado desde el 5-jun | Por el kill switch (H6) no hay trades nuevos → no hay nada que aprender. |

Conclusión Steenbarger: hay journaling y métricas (bien), pero el ciclo de mejora optimiza la función equivocada (Sharpe sobre 5-8 trades = ruido) y aplica correcciones contradictorias. El fitness debería ser **expectativa neta de fees sobre series de ≥25 trades**, con cambios out-of-sample.

---

## 4. Plan de corrección priorizado

> **Estado 2026-06-09 (2ª tanda):** P0 y P1 completos. P2 también implementado: 9 (defensive mode ya no toca sl_pct — reflection/ATR mandan; fitness del tournament = expectativa neta con mínimo 25 trades y cadencia 25 trades/24h), 10 (OnlineML reseteados — pesos anti-predictivos archivados en `.reset-backup-2026-06-09` — y gate nuevo: ≥30 samples + accuracy >50% para opinar), regla del 6% mensual de Elder en el bot spot, y `USE_BNB_FEE` por variable de entorno. Pendientes: activar fee BNB en Binance (acción del usuario), migración a órdenes limit, poda de filtros con datos (punto 11), backtest en máquina del usuario (punto 12). **Requiere reiniciar el bot / deploy.**

### P0 — Hoy (detener el sangrado)
1. **Volver risk_level a 2** (o pausar el spot real). Con expectativa −$0.18/trade, cada trade extra es pérdida esperada.
2. **Arreglar el kill switch de futures**: mover el reset diario de `realized_pnl_today` al inicio de `_risk_checks_pass` (o a un chequeo de rollover en el loop). Una línea, desbloquea todo el módulo de agentes.
3. **Bajar `ACTIVATION_THRESHOLD` de Claude a ~0.20** (o eliminar el gate) para que la capa vuelva a operar, con su rate-limit ya existente.

### P1 — Esta semana (recuperar edge)
4. **Filtro de marea (Elder, Pantalla 1)**: calcular tendencia en 1h y 4h (EMA50/200 + ADX). **Prohibir longs si la marea 4h es bajista.** Esto reemplaza media docena de filtros parche (correlación BTC, breadth, adaptive, etc.) con una sola regla correcta — y habría evitado la mayoría de los 26 stop-outs.
5. **Stops por ATR, no por slider**: SL = 1.5-2× ATR(14) del par (Murphy/reflection ya lo detectó: SL 1.1% con ATR 1% = stop dentro del ruido). TP por estructura o trailing, no un múltiplo fijo inalcanzable.
6. **Quitar el cost_drag del disparo de SL/TP** (dejarlo solo en reporting) — hoy acerca el stop y aleja el target a la vez.
7. **Arreglar noticias de verdad** (tu pedido explícito):
   - Mapear símbolo → nombres reales: SOL→"solana", XRP→"xrp ripple", LINK→"chainlink", ADA→"cardano", AVAX→"avalanche", BNB→"bnb binance", DOT→"polkadot", ETH→"ethereum".
   - Usar las categorías de CryptoCompare (`?categories=SOL,ETH...`) en vez de filtrar por substring.
   - Agregar **calendario macro** (FOMC/CPI/NFP): bloquear entradas ±1h alrededor de eventos de alto impacto.
   - Subir el peso del sentiment solo cuando `confidence` de fuentes sea alta; hoy un score muerto diluye la señal.
8. **Fees**: activar descuento BNB (`use_bnb_fee=True` + saldo BNB) y migrar entradas a órdenes limit/maker. Con 37% de la pérdida en comisiones, esto solo ya mueve la expectativa ~+$0.07/trade.

### P2 — Próximas 2 semanas (que el aprendizaje aprenda)
9. **Resolver el conflicto reflection vs defensive_mode**: defensive mode no debe tocar `sl_pct` (que quede en manos de ATR); que solo suba `min_confidence` y reduzca sizing. Fitness del tournament → expectativa neta por serie de ≥25 trades, no Sharpe de 5.
10. **Resetear los OnlineML** (accuracy 30-37% es peor que nada) y no usar su score hasta acumular ≥30 samples con accuracy >50% out-of-sample.
11. **Podar filtros**: eliminar `DEAD_HOURS_UTC` (n=4 por ventana es ruido), consolidar los 4 filtros BTC en el filtro de marea del punto 4, y registrar por cada filtro cuántas señales bloquea y el PnL hipotético de lo bloqueado (para decidir con datos).
12. **Validación antes de real** (Steenbarger/Douglas): backtest con fees reales → profit factor >1.3 → 25+ trades en paper con expectativa positiva → recién entonces real con sizing mínimo.

### Reglas permanentes (de los libros, hoy violadas)
- Regla 6% de Elder: si el mes acumula −6% del capital, el bot se pausa solo hasta el mes siguiente. **No existe hoy** (el circuit breaker es diario y de $10).
- Douglas: el risk_level no debe poder subirse mientras la expectativa de las últimas 25 operaciones sea negativa (gate automático en el endpoint del dashboard).
- Hougaard: monitorear en el dashboard la relación ganancia media / pérdida media — si cae bajo 1.0, alerta roja, porque con WR 32% necesitás ≥2.1.

---

## 5. Qué responde esto a tus tres preguntas

1. **¿Por qué pierde desde hace un mes?** No es un bug puntual: es un sistema long-only operando contra la marea, con stops dentro del ruido (ATR), TP inalcanzable, fees de taker, y 15 filtros sobreajustados que se contradicen. La expectativa es −$0.18/trade; operar más solo acelera la pérdida.
2. **¿Tiene en cuenta las noticias?** Prácticamente no: la búsqueda de noticias no encuentra nada para 7 de 9 pares por un bug de términos de búsqueda, no hay calendario macro, y el peso de la capa es 12-15%. Punto 7 del plan lo corrige.
3. **¿Aprenden los dos agentes?** El mecanismo existe y registra datos, pero: el ML online es anti-predictivo (30-37% accuracy), el tournament hizo 1 crossover en 5 semanas, reflection nunca aplicó nada y defensive_mode revierte lo que reflection sugiere. Además están **congelados desde el 5-jun** por el bug del kill switch. Aprenden de muestras demasiado chicas y optimizan la métrica equivocada.
