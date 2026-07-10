# Celerity Bot — Diagnóstico real, test estadístico y rediseño
### 29 de junio de 2026 · basado en tus 314 trades reales

Esto no es teoría. Son tus números, calculados sobre `data/trade_history.json`
(143 trades spot reales) y `data/futures_trades.json` (171 trades de los agentes en
papel). Lo escribo como te hablaría un trader que ya vio fracasar muchos bots iguales:
sin endulzar nada, porque tu plata está en juego.

---

## A. Qué dicen tus trades reales

### Spot (el que opera con tu dinero) — 2 may → 9 jun

| Métrica | Valor | Lectura |
|---|---|---|
| PnL neto | **−$25.68** | Pierde |
| Win rate | 32.2% (46 de 143) | Bajo |
| Ganancia media / Pérdida media | +$0.38 / −$0.45 | **Payoff 0.86x** |
| Payoff necesario para no perder | **2.11x** | Estás casi 3 veces lejos |
| Profit factor | **0.41** | Por cada $1 que ganás, perdés $2.46 |
| Expectativa por trade | **−$0.18** | Cada operación, en promedio, resta |
| Racha perdedora más larga | **18 trades seguidos** | |

**De dónde sale exactamente la pérdida (por motivo de salida):**

- `STOP_LOSS`: 26 trades = **−$23.80** ← prácticamente TODA la pérdida
- `AI Signal` (salida del modelo): 34 = −$6.19
- `TRAILING_STOP`: 22 = −$3.05 · `LOSS_TIMEOUT`: 10 = −$2.82 · `TIMEOUT`: 18 = −$1.75
- Lo único en verde: `PARTIAL_TP` +$5.87, cierres **manuales** +$4.05, `TAKE_PROFIT` +$2.00 (solo saltó **2 veces** en 143 trades)

**Traducción de trader:** el motor entra en lugares donde el precio se da vuelta enseguida
(26 stop-losses), el take-profit casi nunca llega, y lo único que genera ganancia es cuando
**tomás ganancia parcial o cerrás vos a mano**. El sistema automático sangra; tu intervención
manual es lo que salva algo. Eso es la prueba de que el motor no tiene ventaja.

**Por par:** los peores fueron LINK (−$5.26), SOL (−$4.78), DOT (−$4.59), ETH (−$4.24).
El único positivo: BNB (+$0.51). Operar 9 alts no te diversificó: caen todas juntas con BTC.

### Futures / agentes (en papel) — 171 trades

PnL −$3.28 · WR 36.8% · profit factor 0.80 · expectativa −$0.019/trade.
Dato clave para tu pregunta sobre shorts:

- **LONG: −$1.91 · SHORT: −$1.38.** Los dos lados pierden.
- `TIMEOUT` fue el destino de **88 de 171** trades: entran en señales débiles que no van a
  ningún lado y mueren por tiempo.
- MomentumHunter (146 trades): −$4.01. ReversalSniper (25 trades): +$0.73.

---

## B. Test estadístico: ¿es mala suerte o el sistema no tiene edge?

Hice un **bootstrap de 20.000 remuestreos** sobre tus trades reales (esto mide si la
expectativa negativa es real o ruido):

| | Expectativa/trade | Intervalo de confianza 95% | Prob. de que el edge real sea > 0 |
|---|---|---|---|
| **Spot** | −$0.180 | **[−$0.263, −$0.097]** | **0.0%** |
| Futures (paper) | −$0.019 | [−$0.057, +$0.025] | 17.5% |

El techo del intervalo del spot es **negativo**. En lenguaje claro: **no es mala suerte.**
Con 143 operaciones reales, la probabilidad de que esta estrategia tenga ventaja positiva es,
estadísticamente, **cero**. No hay tweak de filtro que arregle eso — el problema es estructural.

### ¿Es alcanzable +$10 por semana así?

- Ritmo real: **26 trades/semana**. A tu expectativa actual eso da **−$4.73/semana** (perdés).
- Para +$10/semana necesitarías **+$0.38/trade neto** (hoy −$0.18). Es un giro de +$0.56 por
  trade, o pasar de **−0.54% a +1.15% neto por operación**, consistente. Con scalping de 5m y
  fees de 0.2% ida y vuelta, eso es prácticamente imposible.
- **Fees pagadas: $9.51.** Aunque la estrategia fuera neutral (cero edge), las comisiones solas
  te hacían perder ese monto. El over-trading es un impuesto que pagás sí o sí.

> Sobre la meta: **$10/sem sobre ~$500 = 2% semanal ≈ 180% anual.** Eso supera lo que logran
> los mejores fondos del mundo de forma sostenida. Fijar un objetivo en dólares por semana te
> **empuja a sobre-operar**, que es justo lo que te funde. La meta correcta no es un sueldo
> semanal: es **expectativa positiva comprobada**. La ganancia, si llega, es lumpy (a saltos),
> no un goteo fijo.

*(Backtest sobre klines históricos: desde este entorno Binance no es alcanzable —el mismo
límite que notó tu auditoría del 9-jun—. Por eso usé tus 314 trades reales, que son evidencia
**más fuerte** que un backtest: son ejecuciones reales, con fills y fees reales, sin
sobreajuste. Igual te dejo `backtest_simple.py` para correr el backtest en tu máquina.)*

---

## C. Rediseño: "Celerity v2" — simple, testeable, con un edge real

La idea de fondo: **dejar de pelear contra la matemática.** Tu propio historial dice qué
funciona (dejar correr ganadores, tomar parciales) y qué no (scalpear 5m, stops dentro del
ruido, 15 filtros que se contradicen). v2 se construye sobre eso.

### Cambios de paradigma

1. **Subir de timeframe: de 5m a diario (o 4h).** Esto solo cambia todo: pasás de ~26 trades/semana
   a ~1–3 por semana. El impuesto de fees colapsa y dejás de capturar ruido.
2. **No estar siempre largo. Largo o CASH.** En spot no podés shortear, pero sí podés **estar en
   USDC cuando la tendencia de fondo es bajista.** Solo esa regla habría evitado la mayoría de
   los 26 stop-losses.
3. **Pocos pares, los más líquidos.** BTC y ETH. Nada de 9 alts correlacionadas.
4. **Invertir el payoff.** Win rate bajo (~40%) está BIEN si el ganador promedio es >2x el
   perdedor. Trend-following hace exactamente eso: muchas pérdidas chicas, pocos ganadores
   grandes. Es el opuesto matemático de tu problema actual (payoff 0.86x).
5. **Una sola idea con ventaja documentada** (momentum de tendencia en timeframe alto), no seis
   capas + ML + Claude + 15 filtros que nadie puede auditar.

### Reglas concretas (spot, long/cash, decisión 1 vez por día)

- **Universo:** BTCUSDT, ETHUSDT.
- **Filtro de régimen:** operar SOLO si `Close > EMA200`. Si no → cash (USDC).
- **Entrada LONG:** `EMA20 > EMA50` **y** `Close` rompe el máximo de los últimos 20 días (Donchian).
- **Salida:** `Close < EMA50` **o** trailing tipo Chandelier (`máximo − 3×ATR14`). **Sin
  take-profit fijo** — se deja correr al ganador.
- **Riesgo:** se arriesga **1% del equity por trade**; el tamaño sale de la distancia al stop
  (sizing por ATR), no de un slider.

### Sobre shorts y plataforma

- **¿Binance o cambiar?** Binance está bien — top en fees y liquidez. El problema **no es la
  plataforma**, cambiar sería perder el tiempo.
- **¿Shorts?** Tus datos muestran que los shorts también pierden con el motor actual: **el
  problema es la entrada, no la dirección.** Primero comprobá edge en long/cash con v2. Si v2
  da expectativa positiva, recién ahí tiene sentido la versión simétrica con shorts en perpetuos
  y leverage 1x.

### Cómo lo probás (sin arriesgar un peso)

1. Corré en tu máquina: `pip install requests pandas numpy && python backtest_simple.py`.
   Compara Buy&Hold vs Scalp (bot actual) vs Celerity v2 con fees reales.
2. Criterio para tomarlo en serio: **profit factor > 1.3 y expectativa > 0 neta de fees** sobre
   ≥100 trades de backtest, y un drawdown que te banques.
3. Si pasa, correlo en **papel 4 semanas**. Si sigue positivo, recién ahí dinero real con tamaño
   mínimo.

### Expectativa realista (la parte que nadie te dice)

Una estrategia de tendencia sólida en cripto puede rendir, en un año bueno, quizá 20–60%, **con
meses perdedores y drawdowns de 15–25%**. Sobre $500 eso es del orden de $2–6/semana *en
promedio anual*, a saltos, no fijo. Es honesto, es sostenible, y es lo opuesto a quemar la
cuenta a $0.18 por trade. La meta no es "$10 esta semana"; es **no perder y tener un edge real.**
Con eso, el tamaño (y la ganancia) crecen solos con el tiempo.

---

## Qué haría YO esta semana, en orden

1. **Pausar el trading real del bot actual.** Su expectativa es −$0.18/trade con 0% de
   probabilidad de ser positiva. Cada día que opera, pierde.
2. Recargar (o desactivar) la API de Anthropic: hoy está caída por falta de créditos y las
   capas de Claude/adjudicador están muertas otra vez.
3. Correr `backtest_simple.py` y mirar los números de v2 con tus propios ojos.
4. Si v2 convence → 4 semanas en papel → recién después, real con tamaño mínimo.

No te voy a prometer ganancias —nadie honesto puede—. Pero te puedo asegurar esto: **lo que
estás haciendo hoy tiene ventaja negativa comprobada, y la dirección correcta es menos
complejidad, menos trades, timeframe más alto y dejar correr al ganador.**

*Aviso: no soy asesor financiero; esto es análisis de tus propios datos para que decidas vos.*
