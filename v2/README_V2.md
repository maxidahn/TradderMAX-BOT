# Celerity v2 — Bot de TradingView + Binance Futures

Bot **mínimo y auditable** (~500 líneas vs las 14.000 del bot viejo). La estrategia
vive en TradingView (Pine Script); el bot solo **valida y ejecuta**. Arranca en
**paper** (no toca tu dinero) y opera **long + short** en perpetuos con leverage 1x.

```
v2/
  celerity_v2.pine   → la ESTRATEGIA (va en TradingView)
  app_v2.py          → servidor que recibe los webhooks de TradingView
  executor_v2.py     → ejecuta en Binance Futures (paper/live) + riesgo + estado
  config_v2.py       → configuración por variables de entorno
  .env.example       → plantilla de claves (copiá a .env)
  Procfile           → para deploy en Railway
```

## ▶ Modo LOCAL (empezá por acá — sin TradingView, sin servidor)

Para validar rápido en tu máquina, la estrategia v2 corre **dentro del bot**:
baja velas de Binance, calcula las señales y ejecuta en paper. No necesitás
TradingView ni URL pública.

```bash
cd v2
pip install -r requirements.txt
cp .env.example .env            # opcional: sin claves igual funciona en paper (usa datos públicos)
python run_local.py --once      # una sola pasada de prueba
python run_local.py             # loop continuo (revisa cada 15 min)
```

Config del modo local (en `.env` o variables de entorno):
- `V2_TIMEFRAME=4h` → temporalidad de decisión (usá `4h` o `1d`; NO 5m).
- `V2_POLL_SECONDS=900` → cada cuánto revisa (15 min).

Cada pasada te imprime, por par: precio, posición actual y la decisión
(LONG/SHORT/CLOSE/esperar) con el motivo. El estado (equity, PnL, historial)
queda en `v2/data/`. Cuando estés conforme con lo que ves en paper, recién ahí
pasás a live o al modo TradingView de abajo.

> Archivos del modo local: `strategy_v2.py` (la estrategia) + `run_local.py` (el loop).
> Reusa el mismo `executor_v2.py` y la misma gestión de riesgo que el modo webhook.

---

## Cómo "conversan" TradingView y el bot (modo avanzado, para después)

```
  TradingView (Pine Script v2)
        │  cuando se cumple la regla, dispara una ALERTA
        │  con un JSON: {"secret","action":"LONG/SHORT/CLOSE","symbol","sl_pct"}
        ▼  (HTTP POST = webhook)
  app_v2.py  /webhook/tradingview
        │  1) valida el secreto   2) valida el símbolo (whitelist)
        ▼
  executor_v2.py → abre/cierra en Binance Futures (o simula en paper)
```

TradingView **no** ejecuta nada: solo manda el aviso. La orden la pone tu bot.

---

## Paso a paso

### 1. Probar local (paper)
```bash
cd v2
pip install -r requirements.txt
cp .env.example .env          # editá .env: poné un V2_WEBHOOK_SECRET largo y único
python app_v2.py              # arranca en http://localhost:8080
```
Probá que vive:
```bash
curl localhost:8080/                                  # healthcheck
curl -X POST localhost:8080/webhook/tradingview \
  -H "Content-Type: application/json" \
  -d '{"secret":"TU_SECRETO","action":"LONG","symbol":"BTCUSDT","sl_pct":3}'
curl localhost:8080/status                            # ver la posición paper abierta
```

### 2. Subir a un servidor (para que TradingView lo alcance)
TradingView necesita una **URL pública**. Tu repo ya usa **Railway**:
- Creá un servicio nuevo apuntando a la carpeta `v2/`.
- Cargá las variables del `.env` en Railway (Settings → Variables).
- Railway te da una URL tipo `https://celerity-v2.up.railway.app`.
- Tu webhook será: `https://celerity-v2.up.railway.app/webhook/tradingview`

### 3. Cargar la estrategia en TradingView
1. Abrí TradingView → gráfico de **BTCUSDT** (Binance) en temporalidad **4h o 1D**.
2. Pine Editor → pegá `celerity_v2.pine` → **Add to chart**.
3. En los ajustes del script, poné el mismo **secreto** que tu `.env` (`V2_WEBHOOK_SECRET`).

### 4. Crear la alerta (esto es lo que dispara las órdenes)
1. Botón **Alerta** (reloj) → Condición: **Celerity v2**.
2. En "Opciones", elegí **Order fills only** (solo cuando hay entrada/salida real).
3. En **Mensaje** poné exactamente:
   ```
   {{strategy.order.alert_message}}
   ```
4. En **Notificaciones → Webhook URL** pegá la URL de tu bot.
5. Crear. Repetí para **ETHUSDT** (una alerta por par).

> ⚠️ Las alertas con webhook requieren un plan **pago** de TradingView. El plan
> gratis no las manda.

---

## Seguridad y riesgo (lo que ya trae)

- **Secreto del webhook**: cualquier POST sin el secreto correcto → rechazado (403).
- **Whitelist de pares**: solo opera los de `V2_SYMBOLS`. Cualquier otro → rechazado.
- **Riesgo por trade**: arriesga `V2_RISK_PCT` (1%) del equity; el tamaño sale del SL.
- **Kill switch diario**: si la pérdida del día supera `V2_MAX_DAILY_LOSS_PCT` (4%),
  no abre nada más hasta el día siguiente.
- **Máx posiciones simultáneas**: `V2_MAX_POSITIONS` (2).
- **Reversión**: si llega una señal opuesta a una posición abierta, la cierra y abre la nueva.
- **Leverage 1x** por defecto.

## El plan correcto (no te saltees esto)

1. **Paper 4 semanas.** Mirá `/status`: que la expectativa sea **positiva** y el
   profit factor **> 1.3** antes de tocar dinero real.
2. Si pasa → poné `V2_PAPER=false` y empezá con `V2_MAX_NOTIONAL` chico.
3. Claves de Binance: permiso **Futures + Trading**, **NUNCA retiro**.

No hay garantía de ganar. Pero esto es simple, auditable y sin las trampas que
hacían perder al bot viejo (over-trading, stops dentro del ruido, payoff invertido).
La estrategia (Pine) la podés ajustar y **backtestear dentro de TradingView** antes
de activar una sola alerta.
