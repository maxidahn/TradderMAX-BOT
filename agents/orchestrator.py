"""
Agents Orchestrator
=====================
Coordina todo el sistema multi-agente:

  Cada tick (cada N segundos):
    1. Para cada símbolo: pide decisión a cada agente
    2. Si los agentes coinciden → ejecuta acción
    3. Si discrepan (LONG vs SHORT) → llama Adjudicator
    4. Si confidence < min_confidence → FLAT
    5. Aplica filtros macro (funding guard, kill switch, max positions)
    6. Si decide entrar → FuturesTrader abre posición
    7. Registra TODO en ReplayBuffer

  Loop secundario (cada sl_tp_seconds):
    - Chequea SL/TP/trailing/timeout de posiciones abiertas

  Cada cierto tiempo:
    - Tournament: corre crossover si toca
    - Aplica funding payments cuando Binance lo liquida

El orchestrator corre en su propio thread y NO bloquea al spot bot.
"""

import json
import logging
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from agents.base_agent import BaseAgent, AgentDecision, Action
from agents.momentum_agent import MomentumAgent
from agents.reversion_agent import ReversionAgent
from agents.replay_buffer import ReplayBuffer
from agents.tournament import Tournament
from agents.adjudicator import Adjudicator
from agents.online_ml import OnlineLearner
from agents.reflection import Reflector
from agents.contagion import ContagionBus
from agents.bandit import BanditPool
from futures_trader import FuturesTrader
from perpetuals_data import PerpetualsData

logger = logging.getLogger("celerity.orchestrator")

DATA_DIR = os.getenv("DATA_DIR", "data")
SAFETY_PHASE_FILE = os.path.join(DATA_DIR, "live_safety_phase.json")


class AgentsOrchestrator:

    def __init__(self, app_config, telegram=None):
        self.cfg     = app_config
        self.fc      = app_config.futures
        self.ac      = app_config.agents
        self.tg      = telegram
        self.running = False
        self._thread = None
        self._log_messages: List[dict] = []
        self._log_lock = threading.Lock()

        # ── Components ───────────────────────────────────────────────────────
        self.futures_trader = FuturesTrader(app_config.binance, self.fc)
        self.perp_data      = PerpetualsData()
        self.replay         = ReplayBuffer()
        self.adjudicator    = Adjudicator()

        # ── Aprendizaje acelerado: piezas opcionales habilitadas por config ─
        # OnlineLearner por agente (#2)
        ml_enabled = getattr(self.ac, "online_ml_enabled", True)
        self.online_learners: Dict[str, OnlineLearner] = {}
        if ml_enabled:
            self.online_learners["MomentumHunter"] = OnlineLearner("MomentumHunter")
            self.online_learners["ReversalSniper"] = OnlineLearner("ReversalSniper")

        # Reflector con Claude (#3)
        if getattr(self.ac, "reflection_enabled", True):
            self.reflector = Reflector(
                model=getattr(self.ac, "reflection_model", "claude-haiku-4-5-20251001"),
                apply_threshold=getattr(self.ac, "reflection_apply_threshold", 3),
                max_delta_pct=getattr(self.ac, "reflection_max_delta_pct", 0.10),
            )
        else:
            self.reflector = None

        # ContagionBus (#5)
        if getattr(self.ac, "contagion_enabled", True):
            self.contagion = ContagionBus(
                boost_winner=getattr(self.ac, "contagion_boost_winner", 0.15),
                boost_loser=getattr(self.ac, "contagion_boost_loser", -0.10),
                lookback_minutes=getattr(self.ac, "contagion_lookback_minutes", 60),
                similarity_threshold=getattr(self.ac, "contagion_similarity_threshold", 0.75),
            )
        else:
            self.contagion = None

        # ── Agents — ahora con OnlineLearner + ContagionBus inyectados ──────
        ml_weight = getattr(self.ac, "online_ml_weight", 0.25)
        ml_min_samples = getattr(self.ac, "online_ml_min_samples", 8)
        self.momentum = MomentumAgent(
            params=self.ac.momentum_initial,
            perpetuals_data=self.perp_data,
            replay_buffer=self.replay,
        )
        # Atributos de aprendizaje acelerado se inyectan post-init para mantener
        # la firma compatible con bandit (que crea agentes sin estos extras).
        self.momentum.online_learner = self.online_learners.get("MomentumHunter")
        self.momentum.contagion_bus  = self.contagion
        self.momentum.online_ml_weight = ml_weight
        self.momentum.online_ml_min_samples = ml_min_samples

        self.reversion = ReversionAgent(
            params=self.ac.reversion_initial,
            perpetuals_data=self.perp_data,
            replay_buffer=self.replay,
        )
        self.reversion.online_learner = self.online_learners.get("ReversalSniper")
        self.reversion.contagion_bus  = self.contagion
        self.reversion.online_ml_weight = ml_weight
        self.reversion.online_ml_min_samples = ml_min_samples

        self.agents: List[BaseAgent] = [self.momentum, self.reversion]

        # ── Bandit pool: variantes paper paralelas (#4) ──────────────────────
        if getattr(self.ac, "bandit_enabled", True):
            self.bandit = BanditPool(
                self.ac,
                principals=self.agents,
                agent_class_map={
                    "MomentumHunter":  MomentumAgent,
                    "ReversalSniper":  ReversionAgent,
                },
                perpetuals_data=self.perp_data,
            )
        else:
            self.bandit = None

        # ── Tournament — carga params persistidos + integra bandit ──────────
        self.tournament = Tournament(self.ac, self.replay, self.agents, bandit=self.bandit)

        # ── Safety phase: primeras 72h en live se fuerza leverage=1 ─────────
        self._live_started_at = self._load_safety_phase()

        # ── Map open position symbol → replay entry id (para attach_outcome) ─
        self._open_entry_ids: Dict[str, str] = {}

        # ── Last decisions (para dashboard) ──────────────────────────────────
        self.last_decisions: Dict[str, dict] = {}   # symbol → {momentum: {...}, reversion: {...}, resolved: {...}}

    # ─── Logging helper ──────────────────────────────────────────────────────

    def _log(self, level: str, msg: str):
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": msg}
        with self._log_lock:
            self._log_messages.append(entry)
            if len(self._log_messages) > 150:
                self._log_messages = self._log_messages[-150:]
        getattr(logger, level.lower() if level != "WARN" else "warning", logger.info)(msg)

    def get_logs(self, n: int = 50) -> List[dict]:
        with self._log_lock:
            return list(self._log_messages[-n:])

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Arranca el orchestrator. Si la conexión a Binance falla, lanza un
        thread que reintenta cada 30s hasta lograrlo (robusto a DNS hiccups
        durante el arranque)."""
        if self.running:
            return False
        if not self.fc.enabled:
            self._log("WARN", "Agents module disabled in config (futures.enabled=False)")
            return False

        # Intento inmediato
        if self.futures_trader.connect():
            return self._start_thread()

        # Conexión falló — arrancar retry thread
        self._log("WARN",
            "FuturesTrader could not connect (Binance reachable?). "
            "Retrying every 30s in background. The orchestrator will start "
            "automatically as soon as the connection is restored.")
        retry_thread = threading.Thread(target=self._reconnect_loop, daemon=True)
        retry_thread.start()
        return False

    def _reconnect_loop(self):
        """Background reconnect loop — corre hasta lograr conectar a Binance,
        después arranca el _run_loop normal. Inmune a DNS hiccups en startup."""
        attempt = 0
        while not self.running:
            attempt += 1
            time.sleep(30)
            if self.running:   # alguien arrancó por otro camino
                return
            self._log("INFO", f"Reconnect attempt #{attempt}...")
            if self.futures_trader.connect():
                self._log("INFO", f"Reconnected after {attempt} attempt(s) — starting orchestrator")
                self._start_thread()
                return

    def _start_thread(self) -> bool:
        """Arranca el thread principal del loop. Asume que ya hay conexión OK."""
        self.perp_data.set_client(self.futures_trader.client)
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        mode = "PAPER" if self.fc.paper_trade else "LIVE"
        self._log("INFO", f"AgentsOrchestrator started — mode={mode}, pairs={[p.symbol for p in self.fc.pairs if p.enabled]}")
        return True

    def stop(self):
        self.running = False
        self._log("INFO", "AgentsOrchestrator stopped")

    def toggle_paper_mode(self, paper: bool):
        """Switch entre paper y live. Solo afecta órdenes nuevas.

        Cuando se cambia de PAPER → LIVE, arranca el contador de safety phase
        si todavía no había arrancado. Durante las primeras `safety_phase_hours`
        el leverage está forzado a `safety_phase_max_leverage` (1x default).
        """
        old = self.fc.paper_trade
        self.fc.paper_trade = paper
        self._log("WARN", f"Mode change: PAPER={old} → PAPER={paper}")
        if old is True and paper is False and not self._live_started_at:
            # Primer arranque de live → registra timestamp
            self._live_started_at = time.time()
            self._save_safety_phase()
            self._log("WARN",
                f"🛡️ SAFETY PHASE iniciada: leverage forzado a "
                f"{self.fc.safety_phase_max_leverage}x durante "
                f"{self.fc.safety_phase_hours}h. Después se permite hasta "
                f"{self.fc.max_leverage}x.")

    def _save_safety_phase(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(SAFETY_PHASE_FILE, "w") as f:
                json.dump({
                    "live_started_at":      self._live_started_at,
                    "safety_phase_hours":   self.fc.safety_phase_hours,
                    "max_leverage_during":  self.fc.safety_phase_max_leverage,
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"_save_safety_phase failed: {e}")

    def _load_safety_phase(self) -> Optional[float]:
        if not os.path.exists(SAFETY_PHASE_FILE):
            return None
        try:
            with open(SAFETY_PHASE_FILE) as f:
                d = json.load(f)
            return float(d.get("live_started_at")) if d.get("live_started_at") else None
        except Exception:
            return None

    def safety_phase_active(self) -> bool:
        """¿Estamos dentro del período de protección leverage=1x?"""
        if not self._live_started_at:
            return False
        elapsed_h = (time.time() - self._live_started_at) / 3600.0
        return elapsed_h < self.fc.safety_phase_hours

    def effective_leverage(self, requested: int) -> int:
        """Aplica el cap de safety phase si está activo."""
        if self.safety_phase_active():
            return min(requested, self.fc.safety_phase_max_leverage)
        return min(requested, self.fc.max_leverage)

    # ─── Main loop ───────────────────────────────────────────────────────────

    def _run_loop(self):
        last_tick = 0.0
        last_sl_tp = 0.0
        last_funding_check = 0.0

        self._log("INFO", "Loop running (tick: %ds, SL/TP: %ds)" % (self.fc.tick_seconds, self.fc.sl_tp_seconds))

        while self.running:
            try:
                now = time.time()

                # ── Fast: SL/TP/trailing checks ─────────────────────────────
                if now - last_sl_tp >= self.fc.sl_tp_seconds:
                    closed = self.futures_trader.check_position_exits(
                        max_hold_min=max(self.momentum.params.max_hold_minutes,
                                          self.reversion.params.max_hold_minutes),
                        trailing_after_pct=min(self.momentum.params.trailing_after_pct,
                                                 self.reversion.params.trailing_after_pct),
                        trailing_distance_pct=min(self.momentum.params.trailing_distance_pct,
                                                    self.reversion.params.trailing_distance_pct),
                    )
                    for rec in closed:
                        self._on_position_closed(rec)
                    last_sl_tp = now

                # ── Funding payment application (every 8h Binance liquida) ──
                # Heurística: cada 30 min revisamos si hay funding aplicable
                if now - last_funding_check >= 1800:
                    self._apply_pending_funding()
                    last_funding_check = now

                # ── Tick: agents decide ─────────────────────────────────────
                if now - last_tick >= self.fc.tick_seconds:
                    self._tick()
                    last_tick = now

                    # ── Tournament check ──────────────────────────────────
                    if self.tournament.should_run():
                        event = self.tournament.run()
                        self._log("INFO", f"Tournament: {event.get('event')} — {event.get('reason', '')[:80]}")
                        if event.get("event") == "crossover" and self.tg:
                            try:
                                self.tg._send(
                                    f"🧬 *Agent Tournament*\n"
                                    f"Winner: `{event['winner']}` (Sharpe {event['winner_sharpe']})\n"
                                    f"Loser:  `{event['loser']}` (Sharpe {event['loser_sharpe']})\n"
                                    f"Edge: {event['edge_pct']:.0f}% → params crossed over"
                                )
                            except Exception:
                                pass

                time.sleep(min(self.fc.sl_tp_seconds, self.fc.tick_seconds) / 2)

            except Exception as e:
                self._log("ERROR", f"Loop error: {e}")
                time.sleep(5)

    # ─── Tick: ask agents, resolve, execute ──────────────────────────────────

    def _tick(self):
        for pair in self.fc.pairs:
            if not pair.enabled:
                continue
            try:
                candles = self.futures_trader.get_candles(pair.symbol, interval="5m", limit=200)
                if candles is None or candles.empty:
                    continue

                # ── Bandit (#4): variantes paper paralelas deciden y evalúan ─
                # No bloqueante para el flow principal — solo entrena en paralelo
                if self.bandit:
                    try:
                        self.bandit.tick(pair.symbol, candles)
                    except Exception as bandit_err:
                        logger.debug(f"bandit tick error {pair.symbol}: {bandit_err}")

                # Ask both agents (principales)
                d_mom = self.momentum.decide(pair.symbol, candles)
                d_rev = self.reversion.decide(pair.symbol, candles)

                # Store snapshot for dashboard
                self.last_decisions[pair.symbol] = {
                    "momentum":  self._decision_to_dict(d_mom),
                    "reversion": self._decision_to_dict(d_rev),
                    "resolved":  None,
                }

                # ── Resolve ─────────────────────────────────────────────────
                resolved = self._resolve(d_mom, d_rev, pair.symbol)
                self.last_decisions[pair.symbol]["resolved"] = resolved

                if not resolved or resolved["action"] == "FLAT":
                    continue

                # ── Pre-flight risk checks before executing ─────────────────
                if not self._risk_checks_pass(pair, resolved):
                    continue

                # ── Execute ─────────────────────────────────────────────────
                self._execute(pair, resolved)

            except Exception as e:
                self._log("ERROR", f"{pair.symbol}: tick error — {e}")

    def _decision_to_dict(self, d: AgentDecision) -> dict:
        return {
            "agent":      d.agent_name,
            "action":     d.action.value,
            "confidence": d.confidence,
            "reasoning":  d.reasoning,
            "sl_pct":     d.sl_pct,
            "tp_pct":     d.tp_pct,
            "timestamp":  d.timestamp,
        }

    def _resolve(self, d_a: AgentDecision, d_b: AgentDecision, symbol: str) -> Optional[dict]:
        """
        Resuelve la decisión final entre los dos agentes.
        Returns None si no hay acción que tomar.
        """
        # Caso 1: ambos FLAT → nada que hacer
        if d_a.action == Action.FLAT and d_b.action == Action.FLAT:
            return {"action": "FLAT", "agent": None, "reason": "both FLAT"}

        # Caso 2: uno FLAT, el otro tiene señal → el activo manda (si supera min_conf)
        # Cada agente usa SU PROPIO min_confidence (puede haber cambiado por crossover)
        if d_a.action == Action.FLAT and d_b.action != Action.FLAT:
            agent_b = next((a for a in self.agents if a.name == d_b.agent_name), None)
            min_conf_b = agent_b.params.min_confidence if agent_b else 0.5
            if d_b.confidence < min_conf_b:
                return {"action": "FLAT", "agent": d_b.agent_name,
                        "reason": f"confidence {d_b.confidence:.2f} < min {min_conf_b:.2f}"}
            return self._make_resolved(d_b, source="solo")

        if d_b.action == Action.FLAT and d_a.action != Action.FLAT:
            agent_a = next((a for a in self.agents if a.name == d_a.agent_name), None)
            min_conf_a = agent_a.params.min_confidence if agent_a else 0.5
            if d_a.confidence < min_conf_a:
                return {"action": "FLAT", "agent": d_a.agent_name,
                        "reason": f"confidence {d_a.confidence:.2f} < min {min_conf_a:.2f}"}
            return self._make_resolved(d_a, source="solo")

        # Caso 3: ambos en la misma dirección → consenso
        if d_a.action == d_b.action:
            # Promedia confidences (los dos coinciden → más fuerte)
            avg_conf = (d_a.confidence + d_b.confidence) / 2
            stronger = d_a if d_a.confidence >= d_b.confidence else d_b
            if avg_conf < min(self.momentum.params.min_confidence, self.reversion.params.min_confidence):
                return {"action": "FLAT", "agent": stronger.agent_name,
                        "reason": f"consensus but low confidence ({avg_conf:.2f})"}
            return self._make_resolved(stronger, source="consensus", override_conf=avg_conf)

        # Caso 4: contradicen (LONG vs SHORT) → Adjudicator
        perp_metrics = self.perp_data.get_metrics(symbol)
        perp_dict = asdict(perp_metrics) if perp_metrics else None
        if not self.ac.adjudicator_enabled:
            # Sin adjudicator: el de mayor confidence gana si diff > 0.10, si no FLAT
            if abs(d_a.confidence - d_b.confidence) < 0.10:
                return {"action": "FLAT", "agent": None, "reason": "conflicting + close confidence (no adjudicator)"}
            winner = d_a if d_a.confidence > d_b.confidence else d_b
            return self._make_resolved(winner, source="confidence_only")

        verdict = self.adjudicator.resolve_conflict(d_a, d_b, perp_dict)
        if verdict["winner"] == "BOTH_WAIT":
            return {"action": "FLAT", "agent": None,
                    "reason": f"adjudicator: {verdict['reasoning'][:80]}"}
        winner_decision = d_a if d_a.agent_name == verdict["winner"] else d_b
        return self._make_resolved(winner_decision, source="adjudicator",
                                    extra_reasoning=verdict["reasoning"])

    def _make_resolved(self, d: AgentDecision, source: str,
                        override_conf: Optional[float] = None,
                        extra_reasoning: str = "") -> dict:
        return {
            "action":     d.action.value,
            "agent":      d.agent_name,
            "confidence": override_conf if override_conf is not None else d.confidence,
            "sl_pct":     d.sl_pct,
            "tp_pct":     d.tp_pct,
            "reason":     f"[{source}] {d.reasoning}" + (f" | adj: {extra_reasoning}" if extra_reasoning else ""),
            "features":   d.features,
            "raw_decision": d,
        }

    # ─── Risk checks ─────────────────────────────────────────────────────────

    def _risk_checks_pass(self, pair, resolved: dict) -> bool:
        symbol = pair.symbol
        action = resolved["action"]

        # Kill switch (already in FuturesTrader.open_position, but log here too)
        equity = self.futures_trader.paper_equity if self.fc.paper_trade else self.futures_trader.get_futures_balance_usdt()
        if self.futures_trader.realized_pnl_today <= -self.fc.max_daily_loss_pct / 100.0 * equity:
            self._log("WARN", f"{symbol}: KILL SWITCH — daily loss limit reached")
            return False

        # Funding guard
        if self.fc.funding_guard_enabled:
            m = self.perp_data.get_metrics(symbol)
            if m:
                if action == "LONG" and m.funding_rate > self.fc.max_funding_rate_long:
                    self._log("INFO",
                        f"{symbol}: LONG blocked — funding {m.funding_rate:+.4f}% > {self.fc.max_funding_rate_long}%")
                    return False
                if action == "SHORT" and m.funding_rate < self.fc.min_funding_rate_long:
                    self._log("INFO",
                        f"{symbol}: SHORT blocked — funding {m.funding_rate:+.4f}% < {self.fc.min_funding_rate_long}%")
                    return False

        # Already in position?
        if symbol in self.futures_trader.positions:
            existing = self.futures_trader.positions[symbol]
            # If existing position is in opposite direction → close first (reversal)
            if existing.side != action:
                self._log("INFO", f"{symbol}: existing {existing.side} contradicts {action} → closing first")
                rec = self.futures_trader.close_position(symbol, reason="AGENT_REVERSAL")
                if rec:
                    self._on_position_closed(rec)
            else:
                # Same direction already open → don't pyramid
                return False

        return True

    # ─── Execution ───────────────────────────────────────────────────────────

    def _execute(self, pair, resolved: dict):
        """Abre la posición correspondiente y registra en replay."""
        action = resolved["action"]
        if action not in ("LONG", "SHORT"):
            return

        # Sizing: notional configurado del par, ajustado por confidence
        base_notional = pair.notional_usdt
        conf_mult = 0.6 + 0.4 * resolved["confidence"]    # 0.6x..1.0x según confidence
        notional = max(pair.min_notional, min(pair.max_notional, base_notional * conf_mult))

        # Leverage del par + safety phase cap (1x durante primeras 72h live)
        leverage = self.effective_leverage(pair.leverage)
        if leverage < pair.leverage and not self.fc.paper_trade:
            self._log("INFO",
                f"🛡️ {pair.symbol}: leverage capeado {pair.leverage}x → {leverage}x "
                f"(safety phase activa)")

        d = resolved["raw_decision"]
        position = self.futures_trader.open_position(
            symbol=pair.symbol,
            side=action,
            notional_usdt=notional,
            leverage=leverage,
            agent=resolved["agent"],
            sl_pct=resolved["sl_pct"] or 1.5,
            tp_pct=resolved["tp_pct"] or 3.0,
            metadata={
                "confidence":      resolved["confidence"],
                "reason":          resolved["reason"],
                "features_sample": {k: v for k, v in d.features.items() if isinstance(v, (int, float, bool, str))},
            },
        )
        if position:
            self._log("INFO",
                f"✅ {action} {pair.symbol} by {resolved['agent']} @ ${position.entry_price:.4f} "
                f"notional=${notional:.2f} lev={leverage}x — {resolved['reason'][:80]}")
            # Record decision in replay (with executed=True) and remember entry id
            params_snapshot = asdict(
                self.momentum.params if resolved['agent'] == self.momentum.name else self.reversion.params
            )
            entry_id = self.replay.record_decision(d, executed=True, params_snapshot=params_snapshot)
            self._open_entry_ids[pair.symbol] = entry_id

            if self.tg:
                try:
                    self.tg._send(
                        f"{'🟢' if action == 'LONG' else '🔴'} *{action} {pair.symbol}*\n"
                        f"Agent: `{resolved['agent']}`\n"
                        f"Price: `${position.entry_price:.4f}` | Notional: `${notional:.2f}` | Lev: `{leverage}x`\n"
                        f"SL: `${position.sl_price:.4f}` | TP: `${position.tp_price:.4f}`\n"
                        f"Mode: `{'PAPER' if self.fc.paper_trade else 'LIVE'}`"
                    )
                except Exception:
                    pass
        else:
            self._log("WARN", f"{pair.symbol}: open_position returned None")
            # Still record as not-executed for learning
            params_snapshot = asdict(
                self.momentum.params if resolved['agent'] == self.momentum.name else self.reversion.params
            )
            self.replay.record_decision(d, executed=False, params_snapshot=params_snapshot)

    # ─── Position closed callback ────────────────────────────────────────────

    def _on_position_closed(self, rec):
        """Llamado cuando una posición se cierra (SL/TP/trailing/timeout/manual).

        Aquí se dispara TODA la cadena de aprendizaje acelerado:
          1. ReplayBuffer: attach outcome a la decisión original
          2. OnlineML (#2): partial_fit con (features, profitable)
          3. ContagionBus (#5): publicar evento para que el otro agente lo vea
          4. Reflector (#3): pedir reflexión a Claude → check_and_apply
        """
        # 1. ReplayBuffer
        entry_id = self._open_entry_ids.pop(rec.symbol, None)
        decision_entry = None
        if entry_id:
            self.replay.attach_outcome(entry_id, {
                "pnl_usdt":     rec.pnl_usdt,
                "pnl_pct":      rec.pnl_pct,
                "exit_reason":  rec.reason,
                "exit_price":   rec.exit_price,
                "hold_minutes": rec.hold_minutes,
                "fees":         rec.fees,
                "funding_paid": rec.funding_paid,
            })
            decision_entry = self.replay.get_by_id(entry_id)

        # Recover features for ML / contagion / reflection
        features = {}
        if decision_entry and decision_entry.get("features"):
            features = decision_entry["features"]
        elif rec.metadata and rec.metadata.get("features_sample"):
            features = rec.metadata["features_sample"]

        profitable = rec.pnl_usdt > 0

        # 2. OnlineML: el agente que hizo el trade actualiza su modelo
        learner = self.online_learners.get(rec.agent) if self.online_learners else None
        if learner and features:
            # Sample weight: PnL en magnitud (no signo). Trades grandes pesan más.
            sample_weight = min(3.0, 1.0 + abs(rec.pnl_pct) / 5.0)
            try:
                learner.partial_fit(features, profitable, sample_weight=sample_weight)
            except Exception as ml_err:
                logger.warning(f"OnlineML fit failed for {rec.agent}: {ml_err}")

        # 3. ContagionBus: publica el evento para que el OTRO agente lo vea
        if self.contagion and features:
            self.contagion.publish(
                agent_name=rec.agent,
                symbol=rec.symbol,
                features=features,
                pnl_pct=rec.pnl_pct,
            )

        # 4. Reflector: pide a Claude que analice el trade y sugiera ajuste
        if self.reflector and self.reflector.enabled:
            agent = next((a for a in self.agents if a.name == rec.agent), None)
            if agent:
                try:
                    self.reflector.reflect(rec, decision_entry, asdict(agent.params))
                    # Check si hay 3 cierres consecutivos sugiriendo lo mismo → aplicar
                    applied = self.reflector.check_and_apply(agent)
                    if applied:
                        self._log("WARN",
                            f"🔧 REFLECTION applied: {applied['agent']}.{applied['param']} "
                            f"{applied['from']} → {applied['to']} ({applied['direction']})")
                        if self.tg:
                            try:
                                self.tg._send(
                                    f"🔧 *Reflection applied*\n"
                                    f"`{applied['agent']}.{applied['param']}`: "
                                    f"`{applied['from']}` → `{applied['to']}`\n"
                                    f"_{applied['rationale']}_"
                                )
                            except Exception:
                                pass
                except Exception as r_err:
                    logger.warning(f"Reflection error: {r_err}")

        emoji = "💰" if rec.pnl_usdt >= 0 else "📉"
        self._log("INFO",
            f"{emoji} CLOSED {rec.side} {rec.symbol} ({rec.reason}) by {rec.agent} | "
            f"PnL ${rec.pnl_usdt:+.4f} ({rec.pnl_pct:+.2f}%) | hold {rec.hold_minutes:.0f}min")

        if self.tg:
            try:
                self.tg._send(
                    f"{emoji} *CLOSE {rec.side} {rec.symbol}*\n"
                    f"Reason: `{rec.reason}` | Agent: `{rec.agent}`\n"
                    f"PnL: `${rec.pnl_usdt:+.4f}` (`{rec.pnl_pct:+.2f}%`) | hold `{rec.hold_minutes:.0f}min`"
                )
            except Exception:
                pass

    # ─── Funding application ─────────────────────────────────────────────────

    def _apply_pending_funding(self):
        """
        Aplica el funding actual a posiciones que se mantienen al momento del
        siguiente settlement. Para mantener simple: aplica la fracción proporcional
        del funding rate por cada 30 min de hold (1/16 del rate de 8h).
        """
        for sym, pos in list(self.futures_trader.positions.items()):
            m = self.perp_data.get_metrics(sym)
            if not m or m.funding_rate == 0:
                continue
            # Fracción de funding por cada 30 min (Binance settle es cada 8h)
            frac_rate = (m.funding_rate / 100.0) * (0.5 / 8.0)
            self.futures_trader.apply_funding(sym, frac_rate)

    # ─── Status (for dashboard) ──────────────────────────────────────────────

    def get_status(self) -> dict:
        trader_status = self.futures_trader.get_status()
        leaderboard = self.tournament.get_leaderboard()
        tournament_events = self.tournament.get_recent_events(limit=10)

        # Safety phase info
        safety = {
            "active":         self.safety_phase_active(),
            "started_at":     self._live_started_at,
            "hours_total":    self.fc.safety_phase_hours,
            "max_leverage":   self.fc.safety_phase_max_leverage,
        }
        if self._live_started_at:
            elapsed_h = (time.time() - self._live_started_at) / 3600.0
            safety["elapsed_hours"] = round(elapsed_h, 1)
            safety["remaining_hours"] = round(max(0, self.fc.safety_phase_hours - elapsed_h), 1)

        return {
            "running":      self.running,
            "mode":         "PAPER" if self.fc.paper_trade else "LIVE",
            "enabled":      self.fc.enabled,
            "pairs":        [
                {"symbol": p.symbol, "name": p.name, "enabled": p.enabled,
                 "leverage": p.leverage, "notional": p.notional_usdt}
                for p in self.fc.pairs
            ],
            "trader":       trader_status,
            "leaderboard":  leaderboard,
            "tournament":   {
                "events":   tournament_events,
                "config":   {
                    "every_n_trades":    self.ac.tournament_every_n_trades,
                    "every_hours":       self.ac.tournament_every_hours,
                    "min_edge_pct":      self.ac.tournament_min_edge_pct,
                    "min_trades":        self.ac.min_trades_for_tournament,
                },
            },
            "decisions":    self.last_decisions,
            "adjudicator":  self.adjudicator.get_stats(),
            # ── Aprendizaje acelerado ────────────────────────────────────────
            "online_ml":    {
                name: lr.get_state() for name, lr in self.online_learners.items()
            } if self.online_learners else {},
            "reflection":   self.reflector.get_status() if self.reflector else {"enabled": False},
            "contagion":    self.contagion.get_status() if self.contagion else {"enabled": False},
            "bandit":       self.bandit.get_status() if self.bandit else {},
            "safety_phase": safety,
            "kill_switch":  {
                "active":       trader_status["realized_pnl_today"] <= -self.fc.max_daily_loss_pct / 100.0 * trader_status["equity"],
                "daily_pnl":    trader_status["realized_pnl_today"],
                "equity":       trader_status["equity"],
                "limit_pct":    self.fc.max_daily_loss_pct,
            },
            "logs":         self.get_logs(50),
        }
