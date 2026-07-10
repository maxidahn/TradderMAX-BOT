# El Entrenador de Trading (The Daily Trading Coach) — Brett Steenbarger
*Conocimiento destilado para agentes de trading. Tema: auto-coaching, proceso y mejora continua.*

## Tesis central
El trader debe convertirse en su propio coach: observar su desempeño con datos, identificar patrones (psicológicos y técnicos) y corregirlos con un proceso estructurado. La mejora viene de tratar el trading como un programa de desarrollo deliberado, no de inspiración.

## Principios clave
- **El proceso sobre el resultado**: enfocarse en ejecutar bien, no en el P&L del día. El P&L de corto plazo es ruido.
- **Journaling estructurado**: registrar cada trade con contexto, razón de entrada, emoción/estado, gestión y salida. Sin registro no hay diagnóstico.
- **Métricas de desempeño**: win rate, profit factor, ganancia/pérdida media, drawdown, desempeño por hora del día, por instrumento, por tipo de setup. Los problemas casi siempre se concentran en un subconjunto identificable.
- **Cambio basado en patrones**: identificar el patrón problemático específico (ej. "pierdo más en operaciones tomadas después de 2 ganancias seguidas" = exceso de confianza) y diseñar una regla concreta contra él.
- **Estado fisiológico importa**: fatiga, estrés y sobreexposición degradan la ejecución. Pausas y límites de sesión son herramientas de riesgo.
- **Trabajar fortalezas**: duplicar lo que ya funciona (mejores setups, mejores horarios) rinde más que arreglar todas las debilidades.

## Lecciones operativas seleccionadas (de las 101)
- Tener un plan escrito por trade y por día; lo no planificado no se opera.
- Las rachas perdedoras requieren *reducir* tamaño, no aumentarlo.
- Cuando el contexto de mercado cambia (volatilidad, régimen), los setups dejan de funcionar — detectar el cambio de régimen es prioridad.
- La frustración es señal de gap entre expectativa y realidad: ajustar expectativas con datos.
- Revisar trades ganadores también: ¿se ganó por proceso o por suerte?

## Reglas operativas derivadas (para implementación en bot)
- **Logging exhaustivo**: cada trade debe registrar timestamp, setup, régimen de mercado, parámetros, resultado, slippage y motivo de salida.
- **Análisis de desempeño segmentado**: reportes periódicos por hora, día de semana, símbolo, tipo de señal y régimen de volatilidad. Desactivar segmentos con expectativa negativa persistente.
- **Detección de régimen**: medir volatilidad (ATR, desviación), tendencia/rango, y correlaciones; condicionar estrategias al régimen actual.
- **Circuit breakers**: límite de pérdida diaria, límite de trades consecutivos perdedores por sesión, reducción automática de tamaño tras drawdown.
- **Revisión periódica programada**: ciclo semanal de evaluación de métricas → ajuste de parámetros → validación, documentado.
- **No optimizar sobre ruido**: cambios de estrategia solo con muestra suficiente y fuera de muestra (out-of-sample).
