# Trading en la Zona — Mark Douglas
*Conocimiento destilado para agentes de trading. Tema: psicología y consistencia.*

## Tesis central
El éxito en trading no depende de un mejor análisis sino de un mejor *mindset*. Los mejores traders piensan en probabilidades y han eliminado el miedo y la euforia de su ejecución. El mercado no causa pérdidas: la interpretación errónea del riesgo sí.

## Las 5 verdades fundamentales
1. Cualquier cosa puede pasar en el mercado.
2. No necesitas saber qué pasará después para ganar dinero.
3. Las ganancias y pérdidas se distribuyen aleatoriamente dentro de cualquier edge (ventaja estadística).
4. Un edge solo significa mayor probabilidad de un resultado sobre otro — nunca certeza.
5. Cada momento del mercado es único.

## Los 7 principios de consistencia
1. Defino objetivamente mi edge antes de operar.
2. Predefino el riesgo de cada operación.
3. Acepto completamente el riesgo o no tomo la operación.
4. Actúo sobre mi edge sin reservas ni vacilación.
5. Me pago al mercado cuando me da ganancias (tomo profits según plan).
6. Monitoreo continuamente mi susceptibilidad a cometer errores.
7. Entiendo la necesidad absoluta de estos principios y nunca los violo.

## Pensar en probabilidades
- Cada trade individual es estadísticamente independiente; el resultado de uno no dice nada sobre el siguiente.
- El edge se manifiesta solo sobre una **serie** de operaciones (mínimo 20-25 trades para evaluar).
- Error clásico: cambiar de estrategia tras 2-3 pérdidas consecutivas. Una racha perdedora es esperada estadísticamente dentro de cualquier sistema rentable.
- Métrica correcta: expectativa = (win rate × ganancia media) − (loss rate × pérdida media). Si es positiva sobre la serie, ejecutar sin dudar.

## Los 4 miedos del trader
1. Miedo a perder dinero → entradas tardías, stops demasiado ajustados.
2. Miedo a equivocarse → no aceptar el stop, mover el stop loss.
3. Miedo a perderse el movimiento (FOMO) → entradas impulsivas sin señal.
4. Miedo a dejar dinero en la mesa → no respetar take profits ni trailing.

## Reglas operativas derivadas (para implementación en bot)
- **Riesgo predefinido**: nunca abrir posición sin stop loss y tamaño calculado antes de la entrada.
- **Independencia de trades**: el sizing y las decisiones no deben depender del resultado del trade anterior (no martingala, no "recuperar pérdidas").
- **Evaluación por series**: evaluar la estrategia por bloques de N trades (ej. 25), no trade a trade.
- **Cero discrecionalidad post-entrada**: una vez en posición, solo ejecutar el plan (stop, target, trailing). No "reinterpretar" el mercado.
- **Aceptación del riesgo**: si la pérdida máxima del trade no es tolerable para la cuenta, el tamaño está mal — reducirlo, no esperar suerte.
- **Errores ≠ pérdidas**: una pérdida ejecutando el plan es un buen trade; una ganancia violando el plan es un error. Registrar ambos en el log.
