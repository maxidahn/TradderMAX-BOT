"""
Celerity Trader — Bot Optimizer Agent
======================================
Agente autónomo que analiza el rendimiento del bot, identifica qué filtros
están bloqueando demasiadas señales, y propone (o aplica) mejoras a config.py.

Uso:
  python optimizer_agent.py            → analiza y muestra recomendaciones
  python optimizer_agent.py --apply    → analiza y aplica cambios automáticamente
  python optimizer_agent.py --report   → solo reporte sin Claude
"""

import json
import os
import re
import sys
import ast
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Paths ────────────────────────────────────────────────────────────────────
BOT_DIR   = Path(__file__).parent
LOG_FILE  = BOT_DIR / "logs" / "celerity_bot.log"
DATA_FILE = BOT_DIR / "logs" / "bot_state.json"
CONFIG_PY = BOT_DIR / "config.py"

# ── Parámetros ajustables por el agente ──────────────────────────────────────
TUNABLE_PARAMS = {
    "RSI_BUY_MAX":        {"current": 60.0, "min": 58.0, "max": 68.0, "step": 1.0},
    "VOL_MIN":            {"current": 0.30, "min": 0.20, "max": 0.50, "step": 0.05},
    "MIN_TECH_SCORE":     {"current": 0.12, "min": 0.08, "max": 0.20, "step": 0.02},
    "BREADTH_THRESHOLD":  {"current": 0.55, "min": 0.45, "max": 0.70, "step": 0.05},
    "MAX_DAILY_LOSS":     {"current": -1.50, "min": -3.0, "max": -0.50, "step": 0.25},
    "SELL_DEBOUNCE":      {"current": 3,    "min": 2,    "max": 5,    "step": 1},
    "MIN_HOLD_MIN":       {"current": 30.0, "min": 15.0, "max": 60.0, "step": 5.0},
    "ACTIVATION_THRESHOLD": {"current": 0.55, "min": 0.40, "max": 0.70, "step": 0.05},
}

# ── Análisis de logs ──────────────────────────────────────────────────────────

def parse_logs(hours: int = 48) -> dict:
    """Lee los últimos N horas de log y extrae métricas."""
    if not LOG_FILE.exists():
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    stats = {
        "blocks": defaultdict(int),      # filtro → cuántas veces bloqueó
        "trades_won":  [],
        "trades_lost": [],
        "signals_buy": 0,
        "signals_sell": 0,
        "signals_hold": 0,
        "sell_reasons": defaultdict(int),
        "analysis_count": 0,
    }

    try:
        with open(LOG_FILE, "r") as f:
            for line in f:
                # Parse timestamp
                m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                if not m:
                    continue
                try:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        continue
                except ValueError:
                    continue

                # Signals
                if "signal=BUY" in line:
                    stats["signals_buy"] += 1
                elif "signal=SELL" in line:
                    stats["signals_sell"] += 1
                elif "signal=HOLD" in line:
                    stats["signals_hold"] += 1

                if "Analysis done" in line:
                    stats["analysis_count"] += 1

                # Blocks
                if "BUY bloqueado" in line:
                    # Extract full reason (including parenthetical)
                    m2 = re.search(r"BUY bloqueado — (.+)", line)
                    if m2:
                        reason = m2.group(1).strip()
                        if "CIRCUIT BREAKER" in reason:
                            stats["blocks"]["circuit_breaker"] += 1
                        elif "BREADTH BAJISTA" in reason:
                            stats["blocks"]["breadth_filter"] += 1
                        elif "RSI" in reason and ("overbought" in reason or ">" in reason and "60" in reason):
                            stats["blocks"]["rsi_ceiling"] += 1
                        elif "RSI cayendo" in reason:
                            stats["blocks"]["rsi_direction"] += 1
                        elif "volumen" in reason.lower() or "VOL_MIN" in reason or "liquidez" in reason:
                            stats["blocks"]["volume_min"] += 1
                        elif "Technical score" in reason:
                            stats["blocks"]["tech_min"] += 1
                        elif "Adaptive score" in reason or "régimen" in reason:
                            stats["blocks"]["adaptive_gate"] += 1
                        elif "BTC" in reason:
                            stats["blocks"]["btc_filter"] += 1
                        elif "posiciones" in reason or "máx" in reason:
                            stats["blocks"]["max_positions"] += 1
                        elif "capital insuficiente" in reason or "sizing" in reason:
                            stats["blocks"]["capital_insuf"] += 1
                        elif "cooldown" in reason:
                            stats["blocks"]["cooldown"] += 1
                        elif "Precio extendido" in reason:
                            stats["blocks"]["pullback_filter"] += 1
                        elif "pérdida diaria" in reason:
                            stats["blocks"]["circuit_breaker"] += 1
                        else:
                            stats["blocks"]["other"] += 1

                # Trade fills — NET: $-0.6366 (-1.36%) or NET: +$0.0043 (+0.02%)
                sell_m = re.search(r"SELL filled: (\w+) @.+\(([+-][\d.]+)%\)", line)
                if sell_m:
                    symbol  = sell_m.group(1)
                    pnl_pct = float(sell_m.group(2))
                    if pnl_pct > 0:
                        stats["trades_won"].append({"symbol": symbol, "pnl_pct": pnl_pct})
                    else:
                        stats["trades_lost"].append({"symbol": symbol, "pnl_pct": pnl_pct})

                # Sell reasons
                if "AI Signal" in line and "SELL" in line:
                    stats["sell_reasons"]["ai_signal"] += 1
                elif "STOP_LOSS" in line:
                    stats["sell_reasons"]["stop_loss"] += 1
                elif "TAKE_PROFIT" in line:
                    stats["sell_reasons"]["take_profit"] += 1
                elif "TIMEOUT" in line:
                    stats["sell_reasons"]["timeout"] += 1
                elif "PARTIAL_TP" in line:
                    stats["sell_reasons"]["partial_tp"] += 1

    except Exception as e:
        print(f"[optimizer] Error parsing logs: {e}")

    # Convert defaultdicts
    stats["blocks"] = dict(stats["blocks"])
    stats["sell_reasons"] = dict(stats["sell_reasons"])
    return stats


def compute_summary(stats: dict) -> dict:
    """Calcula métricas de alto nivel."""
    total_trades  = len(stats["trades_won"]) + len(stats["trades_lost"])
    win_rate      = len(stats["trades_won"]) / total_trades * 100 if total_trades > 0 else 0
    avg_win       = sum(t["pnl_pct"] for t in stats["trades_won"])  / max(len(stats["trades_won"]),  1)
    avg_loss      = sum(t["pnl_pct"] for t in stats["trades_lost"]) / max(len(stats["trades_lost"]), 1)
    total_blocks  = sum(stats["blocks"].values())
    buy_signals   = stats["signals_buy"]
    block_rate    = total_blocks / (buy_signals + total_blocks) * 100 if (buy_signals + total_blocks) > 0 else 0

    top_blocker = max(stats["blocks"], key=stats["blocks"].get) if stats["blocks"] else "none"

    return {
        "total_trades":   total_trades,
        "win_rate":       round(win_rate, 1),
        "avg_win_pct":    round(avg_win,  2),
        "avg_loss_pct":   round(avg_loss, 2),
        "total_blocks":   total_blocks,
        "block_rate_pct": round(block_rate, 1),
        "top_blocker":    top_blocker,
        "buy_signals":    buy_signals,
        "blocks_detail":  stats["blocks"],
        "sell_reasons":   stats["sell_reasons"],
    }


# ── Recomendaciones sin Claude ────────────────────────────────────────────────

def rule_based_recommendations(summary: dict) -> list[dict]:
    """
    Reglas heurísticas para ajuste de parámetros.
    Claude puede refinar estas recomendaciones con más contexto.
    """
    recs = []
    blocks = summary.get("blocks_detail", {})
    win_rate = summary.get("win_rate", 0)
    total_trades = summary.get("total_trades", 0)

    # RSI demasiado restrictivo → subir techo
    if blocks.get("rsi_ceiling", 0) >= 5:
        recs.append({
            "param":     "RSI_BUY_MAX",
            "direction": "up",
            "delta":     2.0,
            "reason":    f"RSI ceiling bloqueó {blocks['rsi_ceiling']} señales. RSI 60-65 no es overbought real.",
            "urgency":   "high",
        })

    # Volumen demasiado estricto → bajar mínimo
    if blocks.get("volume_min", 0) >= 4:
        recs.append({
            "param":     "VOL_MIN",
            "direction": "down",
            "delta":     0.05,
            "reason":    f"Volumen mínimo bloqueó {blocks['volume_min']} señales. El cripto tiene volatilidad de volumen alta.",
            "urgency":   "medium",
        })

    # Pullback filter demasiado agresivo
    if blocks.get("pullback_filter", 0) >= 5:
        recs.append({
            "param":     "MIN_TECH_SCORE",
            "direction": "down",
            "delta":     0.02,
            "reason":    f"Pullback filter bloqueó {blocks['pullback_filter']} señales. Puede ser demasiado estricto.",
            "urgency":   "medium",
        })

    # Win rate bajo → aumentar selectividad
    if win_rate < 30 and total_trades >= 5:
        recs.append({
            "param":     "MIN_TECH_SCORE",
            "direction": "up",
            "delta":     0.02,
            "reason":    f"Win rate {win_rate}% con {total_trades} trades — filtros de calidad de señal deben ser más estrictos.",
            "urgency":   "high",
        })

    # Circuit breaker dispara mucho → ampliar límite
    if blocks.get("circuit_breaker", 0) >= 2:
        recs.append({
            "param":     "MAX_DAILY_LOSS",
            "direction": "down",  # más negativo = más permisivo
            "delta":     0.25,
            "reason":    f"Circuit breaker disparó {blocks['circuit_breaker']} veces. Evaluar si el límite es demasiado conservador.",
            "urgency":   "low",
        })

    # Demasiado pocos trades → bajar umbral general
    if total_trades < 2 and summary.get("buy_signals", 0) > 10:
        recs.append({
            "param":     "BREADTH_THRESHOLD",
            "direction": "up",  # más permisivo: acepta más pares negativos
            "delta":     0.05,
            "reason":    "Muy pocos trades ejecutados vs señales generadas. El breadth filter puede estar cortando demasiado.",
            "urgency":   "medium",
        })

    # Win rate alto → aumentar agresividad (más trades)
    if win_rate >= 60 and total_trades >= 5:
        recs.append({
            "param":     "RSI_BUY_MAX",
            "direction": "up",
            "delta":     2.0,
            "reason":    f"Win rate {win_rate}% — la estrategia funciona bien, se puede buscar más actividad.",
            "urgency":   "low",
        })

    return recs


# ── Claude AI Analysis ────────────────────────────────────────────────────────

def claude_analysis(summary: dict, stats: dict) -> Optional[dict]:
    """Llama a Claude para análisis y recomendaciones avanzadas."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[optimizer] ANTHROPIC_API_KEY no configurada — usando solo reglas heurísticas")
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        prompt = f"""Eres un experto en optimización de bots de trading. Analiza el rendimiento de este bot de crypto spot trading (solo posiciones largas, sin apalancamiento) y recomienda ajustes específicos de parámetros.

RENDIMIENTO ÚLTIMAS 48h:
- Total trades ejecutados: {summary['total_trades']}
- Win rate: {summary['win_rate']}%
- Avg ganancia (wins): {summary['avg_win_pct']:+.2f}%
- Avg pérdida (losses): {summary['avg_loss_pct']:+.2f}%
- Señales BUY generadas: {summary['buy_signals']}
- Señales bloqueadas en total: {summary['total_blocks']} ({summary['block_rate_pct']:.0f}% de bloqueo)

FILTROS QUE BLOQUEARON MÁS:
{json.dumps(summary['blocks_detail'], indent=2)}

RAZONES DE SALIDA:
{json.dumps(summary['sell_reasons'], indent=2)}

PARÁMETROS ACTUALES Y RANGOS PERMITIDOS:
{json.dumps(TUNABLE_PARAMS, indent=2)}

CONTEXTO DEL BOT:
- Estrategia: EMA crossover + RSI + Volume + ML + Adaptive Regime
- Pares: 9 altcoins/stablecoins en Binance spot
- Timeframe: velas de 5 minutos
- Capital estimado: ~$300 USD
- Objetivo: win rate > 50% en mercados alcistas

Responde en este JSON exacto (nada más):
{{
  "diagnosis": "<2-3 oraciones sobre el problema principal>",
  "market_assessment": "bullish|bearish|sideways",
  "recommendations": [
    {{
      "param": "<nombre del parámetro>",
      "new_value": <nuevo valor numérico>,
      "reason": "<máximo 80 chars>",
      "priority": "high|medium|low"
    }}
  ],
  "should_pause_trading": true|false,
  "pause_reason": "<razón si debe pausar, vacío si no>",
  "activity_assessment": "too_active|balanced|too_inactive",
  "summary": "<1 oración de resumen ejecutivo>"
}}

Solo incluye recomendaciones para parámetros que realmente necesiten cambio. Máximo 4 recomendaciones."""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",   # Haiku — barato para análisis periódico
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )

        text = msg.content[0].text.strip()
        # Strip markdown
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = re.sub(r':\s*\+(\d)', r': \1', text)
        return json.loads(text.strip())

    except Exception as e:
        print(f"[optimizer] Error en Claude analysis: {e}")
        return None


# ── Aplicar cambios a bot.py ──────────────────────────────────────────────────

BOT_PY = BOT_DIR / "bot.py"

def _apply_bot_py_param(param: str, new_value) -> bool:
    """Modifica un parámetro inline en bot.py."""
    patterns = {
        "RSI_BUY_MAX":       (r"(RSI_BUY_MAX\s*=\s*)[\d.]+", f"\\g<1>{new_value}"),
        "VOL_MIN":           (r"(VOL_MIN\s*=\s*)[\d.]+",      f"\\g<1>{new_value}"),
        "MIN_TECH_SCORE":    (r"(MIN_TECH_SCORE\s*=\s*)[\d.]+", f"\\g<1>{new_value}"),
        "MAX_DAILY_LOSS":    (r"(MAX_DAILY_LOSS\s*=\s*)-?[\d.]+", f"\\g<1>{new_value}"),
        "SELL_DEBOUNCE":     (r"(if consecutive < )[\d]+",     f"\\g<1>{int(new_value)}"),
        "MIN_HOLD_MIN":      (r"(MIN_HOLD_MIN\s*=\s*)[\d.]+",  f"\\g<1>{new_value}"),
        "BREADTH_THRESHOLD": (r"(>= )0\.\d+(.*breadth)", rf"\\g<1>{new_value}\\2"),
    }
    if param not in patterns:
        return False
    try:
        content = BOT_PY.read_text()
        pattern, replacement = patterns[param]
        new_content, n = re.subn(pattern, replacement, content, count=1)
        if n == 0:
            return False
        BOT_PY.write_text(new_content)
        return True
    except Exception as e:
        print(f"[optimizer] Error aplicando {param}: {e}")
        return False


def _apply_claude_agent_param(param: str, new_value) -> bool:
    """Modifica parámetros en claude_agent.py."""
    ca_file = BOT_DIR / "claude_agent.py"
    patterns = {
        "ACTIVATION_THRESHOLD": (r"(ACTIVATION_THRESHOLD\s*=\s*)[\d.]+", f"\\g<1>{new_value}"),
    }
    if param not in patterns:
        return False
    try:
        content = ca_file.read_text()
        pattern, replacement = patterns[param]
        new_content, n = re.subn(pattern, replacement, content, count=1)
        if n == 0:
            return False
        ca_file.write_text(new_content)
        return True
    except Exception as e:
        print(f"[optimizer] Error aplicando {param} en claude_agent.py: {e}")
        return False


def apply_recommendation(param: str, new_value) -> bool:
    """Aplica un cambio de parámetro al archivo correcto."""
    if param in ("RSI_BUY_MAX", "VOL_MIN", "MIN_TECH_SCORE",
                 "MAX_DAILY_LOSS", "SELL_DEBOUNCE", "MIN_HOLD_MIN", "BREADTH_THRESHOLD"):
        return _apply_bot_py_param(param, new_value)
    elif param == "ACTIVATION_THRESHOLD":
        return _apply_claude_agent_param(param, new_value)
    return False


def clamp_value(param: str, value) -> float:
    """Mantiene el valor dentro de los rangos seguros."""
    p = TUNABLE_PARAMS.get(param, {})
    mn, mx = p.get("min", value), p.get("max", value)
    return max(mn, min(mx, value))


# ── Reporte ───────────────────────────────────────────────────────────────────

def print_report(summary: dict, rule_recs: list, claude_result: Optional[dict], applied: list):
    print("\n" + "═" * 60)
    print("  CELERITY BOT — OPTIMIZER REPORT")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  últimas 48h")
    print("═" * 60)

    print(f"\n📊 RENDIMIENTO")
    print(f"  Trades:    {summary['total_trades']}  |  Win rate: {summary['win_rate']}%")
    print(f"  Avg ganancia: {summary['avg_win_pct']:+.2f}%  |  Avg pérdida: {summary['avg_loss_pct']:+.2f}%")
    print(f"  Señales BUY: {summary['buy_signals']}  |  Bloqueadas: {summary['total_blocks']} ({summary['block_rate_pct']:.0f}%)")

    if summary["blocks_detail"]:
        print(f"\n🚧 FILTROS QUE MÁS BLOQUEAN")
        for filt, cnt in sorted(summary["blocks_detail"].items(), key=lambda x: -x[1]):
            bar = "█" * min(cnt, 20)
            print(f"  {filt:<22} {cnt:3d}  {bar}")

    if summary["sell_reasons"]:
        print(f"\n📤 RAZONES DE SALIDA")
        for reason, cnt in sorted(summary["sell_reasons"].items(), key=lambda x: -x[1]):
            print(f"  {reason:<20} {cnt}")

    if claude_result:
        print(f"\n🤖 ANÁLISIS CLAUDE")
        print(f"  {claude_result.get('diagnosis', '')}")
        print(f"  Mercado: {claude_result.get('market_assessment', '?').upper()}")
        print(f"  Actividad: {claude_result.get('activity_assessment', '?').upper()}")
        if claude_result.get("should_pause_trading"):
            print(f"  ⚠️  CLAUDE SUGIERE PAUSAR: {claude_result.get('pause_reason', '')}")

    print(f"\n💡 RECOMENDACIONES")
    all_recs = claude_result.get("recommendations", []) if claude_result else []
    if not all_recs:
        all_recs = [{"param": r["param"], "new_value": None, "reason": r["reason"], "priority": r["urgency"]}
                    for r in rule_recs]

    if not all_recs:
        print("  No se detectaron ajustes necesarios.")
    for rec in all_recs:
        p       = rec.get("param", "?")
        nv      = rec.get("new_value", "auto")
        reason  = rec.get("reason", "")
        prio    = rec.get("priority", "medium")
        icon    = "🔴" if prio == "high" else ("🟡" if prio == "medium" else "🟢")
        applied_tag = " ✅ APLICADO" if p in applied else ""
        print(f"  {icon} {p}: → {nv}  |  {reason}{applied_tag}")

    if claude_result:
        print(f"\n  💬 {claude_result.get('summary', '')}")

    print("\n" + "═" * 60 + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def run(auto_apply: bool = False, report_only: bool = False):
    print("[optimizer] Leyendo logs y calculando métricas...")
    stats   = parse_logs(hours=48)
    summary = compute_summary(stats)

    print("[optimizer] Calculando recomendaciones por reglas...")
    rule_recs = rule_based_recommendations(summary)

    claude_result = None
    if not report_only:
        print("[optimizer] Consultando Claude Haiku para análisis avanzado...")
        claude_result = claude_analysis(summary, stats)

    applied = []
    if auto_apply:
        print("[optimizer] Aplicando cambios automáticamente...")
        recs = claude_result.get("recommendations", []) if claude_result else []
        if not recs:
            recs = [{"param": r["param"], "new_value": None} for r in rule_recs]

        for rec in recs:
            param     = rec.get("param")
            new_value = rec.get("new_value")
            if param not in TUNABLE_PARAMS:
                continue
            if new_value is None:
                # Rule-based: apply delta
                rule = next((r for r in rule_recs if r["param"] == param), None)
                if not rule:
                    continue
                current  = TUNABLE_PARAMS[param]["current"]
                step     = TUNABLE_PARAMS[param]["step"]
                new_value = current + (step if rule["direction"] == "up" else -step)

            new_value = clamp_value(param, new_value)
            ok = apply_recommendation(param, new_value)
            if ok:
                applied.append(param)
                print(f"  ✅ {param} → {new_value}")
            else:
                print(f"  ❌ No se pudo aplicar {param}")

    print_report(summary, rule_recs, claude_result, applied)

    if auto_apply and applied:
        print(f"[optimizer] {len(applied)} parámetro(s) ajustado(s). Reinicia el bot para aplicar.")

    # Guardar resultado para dashboard/historial
    result_file = BOT_DIR / "logs" / "optimizer_last_run.json"
    try:
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.write_text(json.dumps({
            "timestamp": datetime.now().isoformat(),
            "summary":   summary,
            "applied":   applied,
            "claude":    claude_result,
        }, indent=2, default=str))
    except Exception:
        pass

    return summary, claude_result, applied


if __name__ == "__main__":
    auto_apply  = "--apply"  in sys.argv
    report_only = "--report" in sys.argv
    run(auto_apply=auto_apply, report_only=report_only)
