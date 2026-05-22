"""
Celerity Multi-Agent System
============================
Dos agentes especializados que operan Binance Futures en paralelo y aprenden
uno del otro mediante un Tournament evolutivo + Replay Buffer compartido.

Componentes:
  - base_agent.py        → Interfaz común (AgentDecision, BaseAgent)
  - momentum_agent.py    → Agente A: Momentum Hunter (trends + breakouts)
  - reversion_agent.py   → Agente B: Reversal Sniper (mean reversion)
  - replay_buffer.py     → Memoria de todos los trades (features + outcome)
  - tournament.py        → Selección + crossover + mutación cada 24h/30 trades
  - adjudicator.py       → Claude resuelve conflictos cuando ambos disienten
  - orchestrator.py      → Coordina todo en un loop

El módulo es opcional: si `config.futures.enabled = False`, el spot bot funciona
sin verse afectado.
"""

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
from agents.orchestrator import AgentsOrchestrator

__all__ = [
    "BaseAgent",
    "AgentDecision",
    "Action",
    "MomentumAgent",
    "ReversionAgent",
    "ReplayBuffer",
    "Tournament",
    "Adjudicator",
    "OnlineLearner",
    "Reflector",
    "ContagionBus",
    "BanditPool",
    "AgentsOrchestrator",
]
