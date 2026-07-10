# Celerity Trader — Diagnóstico y plan de mejora

_Generado el 2026-06-06 a partir de los datos reales del bot (`data/trade_history.json`, `data/replay_buffer.jsonl`, configuración actual)._

---

## 1. La verdad sobre la meta de "$10 por semana"

No se puede *garantizar* ganancia en trading, y menos en un sistema que **hoy pierde dinero**. Con ~$500 de capital, $10/semana = **2% semanal (~180% anual)**: es agresivo pero plausible *si y solo si* primero logramos **expectativa matemática positiva**. Mientras la expectativa sea negativa, operar más (o más "micro") solo acelera la pérdida.

El objetivo correcto, en orden:

1. **Dejar de perder** (expectativa ≥ 0 neta de comisiones).
2. **Validar en paper 2–4 semanas** que la expectativa positiva es real y estable.
3. *Recién entonces* perseguir el 2% semanal con dinero real y tamaño controlado.

---

## 2. Diagnóstico con tus números reales (133 trades cerrados)

| Métrica | Valor | Lectura |
|---|---|---|
| PnL neto | **−$25.40** | El sistema pierde |
| PnL bruto (sin fees) | −$16.46 | Incluso sin comisiones, pierde |
| **Comisiones pagadas** | **$8.94** | **54% de la pérdida es solo fees** |
| Win rate | **30.8%** | Muy bajo |
| Ganancia media | +$0.36 | |
| Pérdida media | −$0.44 | |
| Expectativa por trade | **−$0.19** | Negativa: cada trade, en promedio, resta |

**Regla de oro:** con 30.8% de aciertos, para no perder cada ganancia debería ser ≥ 2.25× cada pérdida. Hoy es **0.82×**. Está muy lejos.

### Dónde se pierde exactamente (por motivo de salida)

| Motivo | Nº | PnL neto | Comentario |
|---|---|---|---|
| **STOP_LOSS** | 21 | **−$20.50** | Casi toda la pérdida. Entradas que revierten de inmediato |
| AI (señal SELL) | 33 | −$6.27 | Salidas por señal mal cronometradas |
| TIMEOUT | 16 | −$3.23 | Capital atrapado en trades muertos |
| TRAILING_STOP | 22 | −$3.05 | |
| LOSS_TIMEOUT | 10 | −$2.82 | |
| TAKE_PROFIT | 2 | +$2.00 | **El TP casi nunca se alcanza** |
| Manual | 20 | +$4.05 | |
| PARTIAL_TP | 9 | +$4.43 | Lo único que gana de forma consistente |

**Conclusión:** el problema #1 son las **21 entradas que saltan el stop-loss** (−$20.50). Son trades que entran y el precio va en contra casi de inmediato → el modelo de entrada está comprando en *chop* / momentum tardío que revierte. El TP de 5.6% prácticamente nunca llega (solo 2 veces).

### El sistema de agentes (paper) tiene el MISMO problema

Los dos agentes que "aprenden uno del otro" (MomentumHunter y ReversalSniper) en paper trading:

- MomentumHunter: 33% win rate, −0.37% promedio
- ReversalSniper: 25% win rate, −0.54% promedio

Es decir: **el problema no es un motor concreto ni la falta de aprendizaje** (los agentes llevan aprendiendo desde mayo). El problema es que **el modelo de entrada no tiene ventaja (edge) en el régimen de mercado actual.**

---

## 3. Por qué "micro operaciones" es la dirección equivocada (ahora)

En spot de Binance pagás ~0.1% por lado = **0.2% ida y vuelta** por operación (con órdenes market, que es lo que usa el bot hoy). Las comisiones ya son el **54%** de la pérdida.

Cuantas más operaciones pequeñas hagas, más comisiones pagás sobre un edge que hoy es negativo. **Micro-operar un sistema sin edge = perder más rápido.** La dirección correcta es la opuesta: **menos operaciones, de mayor calidad, que superen claramente el coste**, y reducir el propio coste (ver punto 5).

Cuando la expectativa sea positiva y robusta, *ahí sí* tiene sentido subir frecuencia.

---

## 4. Cambios ya aplicados (seguros, reversibles, en modo conservador)

1. **Cooldown post-stop-loss** (`bot.py`): tras saltar un SL en un par, no se reentra ese par por **2 horas**. Ataca directamente el cúmulo de 21 stop-losses (−$20.50). Configurable en `self.POST_SL_COOLDOWN_SEC`.
2. **Nivel de riesgo 7 → 2** (`data/risk_level.json`): alguien/el optimizer lo había subido a 7 (agresivo: 9% del capital por trade, umbral de señal flojo 0.19, opera en mercados volátiles y laterales). A nivel 2 el bot es ultra-selectivo: umbral 0.30, confianza mínima 0.66, 3% del capital por trade, y NO opera en régimen volátil/lateral. Esto frena el sobre-trading mientras validamos.

> Ambos cambios son reversibles. El optimizer **no** modifica el risk_level, así que el reset se mantiene.

---

## 5. Plan recomendado (en orden de impacto)

### Inmediato — detener el sangrado
- [x] Cooldown post-SL + nivel de riesgo conservador (hecho).
- [ ] **Reducir comisiones**: activar el descuento BNB (mantener saldo BNB y `use_bnb_fee=True` → fee 0.1%→0.075%, −25%), y/o migrar de órdenes *market* a *limit* (maker), lo que puede bajar el fee a ~0.02% o menos. _Requiere tu acción en Binance + un cambio de lógica en `trader.py` que conviene validar aparte._
- [ ] Considerar **pausar el trading real** del bot spot hasta validar (vos decidís; no toco posiciones reales ni ejecuto órdenes).

### Corto plazo — recuperar edge en la entrada
- [ ] **Filtro anti-chop**: exigir tendencia real (ADX alto + pendiente de EMA) para entrar; no comprar contra-tendencia ni en rango. Las 21 pérdidas por SL son entradas en mercado sin dirección.
- [ ] **Gate de coste**: rechazar toda señal cuyo movimiento esperado (según ATR / volatilidad reciente) no supere claramente el coste ida-vuelta + slippage. Convierte "micro ops" en algo seguro: solo se opera si el edge esperado paga las comisiones.
- [ ] **Límite de operaciones por día** y por par, además del cooldown.

### Validación — antes de volver a real
- [ ] **Backtest sobre histórico** (necesita descargar klines de Binance; desde mi entorno la API no es alcanzable, conviene correrlo en tu máquina). Métricas objetivo: profit factor > 1.3, expectativa > 0 neta de fees, max drawdown tolerable.
- [ ] **2–4 semanas en paper** confirmando esas métricas en vivo.
- [ ] Solo si los números cierran: volver a real con tamaño mínimo e ir subiendo gradualmente.

### El bucle de aprendizaje (los dos agentes)
- Ya existe y es sofisticado (`contagion.py`, `tournament.py`, `bandit.py`, `online_ml.py`, `reflection.py`). El problema no es que falte aprendizaje, sino **qué optimiza**: si premia frecuencia o PnL bruto, propaga malos hábitos. Conviene que la función de fitness sea **expectativa neta de comisiones** y penalice el sobre-trading. (Pendiente de revisión detallada en la próxima iteración.)

---

## 6. Qué NO voy a hacer

- No voy a prometer ni "garantizar" ganancias.
- No voy a pasar el bot a real ni ejecutar/cerrar operaciones con tu dinero. Esas decisiones son tuyas; yo preparo y valido el código.
