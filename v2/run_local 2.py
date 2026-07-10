"""
Celerity v2 — Runner LOCAL con dashboard (sin TradingView, sin servidor público)
=================================================================================
Corre TODO en tu máquina: baja velas de Binance, calcula la estrategia v2,
ejecuta en paper (o live) y sirve un dashboard en localhost.

    cd v2
    pip install -r requirements.txt
    cp .env.example .env      # opcional: sin claves igual funciona en paper
    python run_local.py           # loop + dashboard en http://localhost:8080
    python run_local.py --once    # una sola pasada por consola (sin dashboard)

Config por entorno:
    V2_TIMEFRAME    temporalidad de decisión (default 4h). Usá 4h o 1d.
    V2_POLL_SECONDS cada cuánto revisa (default 900 = 15 min)
    PORT            puerto del dashboard (default 8080)
"""
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

import pandas as pd

from config_v2 import config
from executor_v2 import ExecutorV2
import strategy_v2

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("celerity.v2.local")

TIMEFRAME = os.getenv("V2_TIMEFRAME", "4h")
POLL = int(os.getenv("V2_POLL_SECONDS", "900"))
PARAMS = strategy_v2.Params()

# Estado compartido entre el loop y el dashboard
LAST_DECISIONS: dict = {}
LAST_EVAL_TS: str = "—"


def get_klines(executor, symbol, interval, limit=400):
    """Velas de Binance Futures como DataFrame. Descarta la vela en formación."""
    try:
        k = executor.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(k, columns=["t", "open", "high", "low", "close", "v",
                                      "ct", "qv", "n", "tb", "tq", "ig"])
        for c in ["open", "high", "low", "close", "v"]:
            df[c] = df[c].astype(float)
        return df.iloc[:-1].reset_index(drop=True)   # solo velas cerradas
    except Exception as e:
        logger.error("klines %s: %s", symbol, e)
        return None


def hard_sl_check(executor):
    """Red de seguridad entre velas: si el precio ya cruzó el SL guardado, cierra ya."""
    for symbol, pos in list(executor.positions.items()):
        price = executor.price(symbol)
        if not price:
            continue
        breached = (pos.side == "LONG" and price <= pos.sl_price) or \
                   (pos.side == "SHORT" and price >= pos.sl_price)
        if breached:
            logger.warning("%s: precio %.6f cruzó SL %.6f → cierre de seguridad", symbol, price, pos.sl_price)
            executor.handle_signal("CLOSE", symbol)


def evaluate(executor):
    global LAST_EVAL_TS
    hard_sl_check(executor)
    for symbol in config.allowed_symbols:
        df = get_klines(executor, symbol, TIMEFRAME)
        if df is None or len(df) < PARAMS.ema_trend + 5:
            logger.info("%s: sin velas suficientes todavía", symbol)
            LAST_DECISIONS[symbol] = {"price": None, "pos": None, "action": None,
                                      "reason": "sin velas suficientes"}
            continue
        current = executor.positions[symbol].side if symbol in executor.positions else None
        dec = strategy_v2.decide(df, current, PARAMS)
        last = float(df["close"].iloc[-1])
        LAST_DECISIONS[symbol] = {"price": last, "pos": current, "action": dec.action,
                                  "reason": dec.reason, "sl_pct": dec.sl_pct}
        logger.info("%s @ %.4f | pos=%s | decisión=%s (%s)",
                    symbol, last, current or "FLAT", dec.action or "—", dec.reason)
        if dec.action:
            res = executor.handle_signal(dec.action, symbol, dec.sl_pct)
            logger.info("   → ejecución: %s", res)
    LAST_EVAL_TS = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def print_status(executor):
    s = executor.status()
    logger.info("═══ ESTADO [%s] equity=$%.2f | PnL hoy=$%+.2f | trades=%d WR=%.0f%% | abiertas=%s",
                s["mode"], s["equity"], s["realized_pnl_today"],
                s["summary"]["trades"], s["summary"]["win_rate"],
                [p["symbol"] + ":" + p["side"] for p in s["open_positions"]] or "ninguna")


# ─── Loop de estrategia (thread) ─────────────────────────────────────────────
def strategy_loop(executor):
    while True:
        try:
            evaluate(executor)
            print_status(executor)
        except Exception as e:
            logger.error("Loop error: %s", e)
        time.sleep(POLL)


# ─── Dashboard (Flask) ───────────────────────────────────────────────────────
def build_dashboard(executor):
    from flask import Flask, jsonify
    app = Flask(__name__)

    @app.get("/api/status")
    def api_status():
        s = executor.status()
        s["decisions"] = LAST_DECISIONS
        s["timeframe"] = TIMEFRAME
        s["poll_seconds"] = POLL
        s["last_eval"] = LAST_EVAL_TS
        s["symbols"] = config.allowed_symbols
        return jsonify(s)

    @app.get("/")
    def index():
        return DASHBOARD_HTML

    return app


DASHBOARD_HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Celerity v2 — Dashboard local</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--tx:#e6edf3;--mut:#8b949e;--grn:#3fb950;--red:#f85149;--acc:#58a6ff}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:1000px;margin:0 auto;padding:20px}
  h1{font-size:20px;margin:0 0 2px} .sub{color:var(--mut);font-size:12px;margin-bottom:18px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:18px}
  .card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px}
  .card .k{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.4px}
  .card .v{font-size:24px;font-weight:600;margin-top:4px}
  table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden;margin-bottom:18px}
  th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--bd);font-size:13px}
  th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.4px}
  tr:last-child td{border-bottom:none}
  .pos{color:var(--grn)} .neg{color:var(--red)} .mut{color:var(--mut)}
  .pill{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600}
  .long{background:rgba(63,185,80,.15);color:var(--grn)} .short{background:rgba(248,81,73,.15);color:var(--red)}
  .flat{background:rgba(139,148,158,.15);color:var(--mut)}
  .badge{padding:2px 8px;border-radius:6px;font-size:11px;font-weight:600}
  .live{background:rgba(248,81,73,.2);color:var(--red)} .paper{background:rgba(88,166,255,.2);color:var(--acc)}
  h2{font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.5px;margin:0 0 8px}
  .kill{background:rgba(248,81,73,.15);border:1px solid var(--red);color:var(--red);padding:8px 12px;border-radius:8px;margin-bottom:14px;font-weight:600}
</style></head>
<body><div class="wrap">
  <h1>🌀 Celerity v2 <span id="mode" class="badge paper">PAPER</span></h1>
  <div class="sub">Dashboard local · timeframe <b id="tf">—</b> · última revisión <span id="eval">—</span> · refresca cada 5s</div>
  <div id="killbox"></div>
  <div class="grid">
    <div class="card"><div class="k">Equity</div><div class="v" id="equity">—</div></div>
    <div class="card"><div class="k">PnL total</div><div class="v" id="pnl">—</div></div>
    <div class="card"><div class="k">PnL hoy</div><div class="v" id="pnltoday">—</div></div>
    <div class="card"><div class="k">Trades / WR</div><div class="v" id="trades">—</div></div>
  </div>
  <h2>Posiciones abiertas</h2>
  <table id="postbl"><thead><tr><th>Par</th><th>Lado</th><th>Entrada</th><th>Notional</th><th>Stop</th><th>PnL abierto</th></tr></thead><tbody></tbody></table>
  <h2>Señales por par (última revisión)</h2>
  <table id="dectbl"><thead><tr><th>Par</th><th>Precio</th><th>Posición</th><th>Decisión</th><th>Motivo</th></tr></thead><tbody></tbody></table>
  <h2>Últimos trades cerrados</h2>
  <table id="histtbl"><thead><tr><th>Par</th><th>Lado</th><th>PnL</th><th>%</th><th>Motivo</th><th>Cierre</th></tr></thead><tbody></tbody></table>
</div>
<script>
const $=id=>document.getElementById(id);
const money=n=>(n>=0?'+$':'-$')+Math.abs(n).toFixed(2);
const cls=n=>n>=0?'pos':'neg';
async function tick(){
  let s; try{ s=await (await fetch('/api/status')).json(); }catch(e){ return; }
  $('mode').textContent=s.mode; $('mode').className='badge '+(s.mode==='LIVE'?'live':'paper');
  $('tf').textContent=s.timeframe; $('eval').textContent=s.last_eval||'—';
  $('equity').textContent='$'+Number(s.equity).toFixed(2);
  const p=s.summary.total_pnl; $('pnl').textContent=money(p); $('pnl').className='v '+cls(p);
  const pt=s.realized_pnl_today; $('pnltoday').textContent=money(pt); $('pnltoday').className='v '+cls(pt);
  $('trades').textContent=s.summary.trades+' / '+s.summary.win_rate+'%';
  $('killbox').innerHTML = s.kill_switch ? '<div class="kill">⛔ KILL SWITCH activo — pérdida diaria máxima alcanzada. No abre nuevas hasta mañana.</div>' : '';
  // posiciones
  let live_price={}; (s.decisions&&Object.entries(s.decisions).forEach(([k,d])=>live_price[k]=d.price));
  $('postbl').querySelector('tbody').innerHTML = (s.open_positions||[]).map(o=>{
    const cur=live_price[o.symbol]; let up='—',upc='mut';
    if(cur){ const g=(o.side==='LONG'?(cur-o.entry_price):(o.entry_price-cur))*o.quantity; up=money(g); upc=cls(g);}
    return `<tr><td>${o.symbol}</td><td><span class="pill ${o.side.toLowerCase()}">${o.side}</span></td>
      <td>$${o.entry_price}</td><td>$${o.notional.toFixed(2)}</td><td class="mut">$${o.sl_price.toFixed(4)}</td>
      <td class="${upc}">${up}</td></tr>`;
  }).join('') || '<tr><td colspan="6" class="mut">Sin posiciones abiertas</td></tr>';
  // decisiones
  $('dectbl').querySelector('tbody').innerHTML = Object.entries(s.decisions||{}).map(([k,d])=>{
    const pos=d.pos||'FLAT'; const act=d.action||'esperar';
    const ac = d.action==='LONG'?'pos':(d.action==='SHORT'||d.action==='CLOSE'?'neg':'mut');
    return `<tr><td>${k}</td><td>${d.price?('$'+d.price):'—'}</td>
      <td><span class="pill ${pos==='LONG'?'long':pos==='SHORT'?'short':'flat'}">${pos}</span></td>
      <td class="${ac}">${act}</td><td class="mut">${d.reason||''}</td></tr>`;
  }).join('') || '<tr><td colspan="5" class="mut">Esperando primera revisión…</td></tr>';
  // historial
  $('histtbl').querySelector('tbody').innerHTML = (s.recent||[]).slice().reverse().map(t=>
    `<tr><td>${t.symbol}</td><td><span class="pill ${t.side.toLowerCase()}">${t.side}</span></td>
      <td class="${cls(t.pnl_usdt)}">${money(t.pnl_usdt)}</td><td class="${cls(t.pnl_pct)}">${t.pnl_pct}%</td>
      <td class="mut">${t.reason}</td><td class="mut">${(t.exit_time||'').slice(5,16).replace('T',' ')}</td></tr>`
  ).join('') || '<tr><td colspan="6" class="mut">Todavía no hay trades cerrados</td></tr>';
}
tick(); setInterval(tick, 5000);
</script></body></html>"""


def main():
    once = "--once" in sys.argv
    logger.info(config.status_line())
    logger.info("Runner LOCAL | timeframe=%s | poll=%ds | once=%s", TIMEFRAME, POLL, once)

    executor = ExecutorV2(config)
    if not executor.connect():
        logger.error("No pude conectar a Binance. Revisá red/claves.")
        sys.exit(1)

    if once:
        evaluate(executor)
        print_status(executor)
        return

    # Loop de estrategia en thread + dashboard en el hilo principal
    threading.Thread(target=strategy_loop, args=(executor,), daemon=True).start()
    app = build_dashboard(executor)
    logger.info("📊 Dashboard en http://localhost:%d", config.port)
    app.run(host="127.0.0.1", port=config.port, threaded=True)


if __name__ == "__main__":
    main()
