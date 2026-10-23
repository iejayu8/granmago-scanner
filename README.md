# GranMago Scanner

Complemento de Home Assistant. Paper trading del scanner GranMago (EMA50 semanal / diario / 4H, estocástico y zona de liquidez).

El bucle corre **dentro del servidor**. Puedes cerrar la página o apagar la pantalla del móvil: las posiciones, el balance y el historial siguen en el addon.

No envía órdenes a ningún exchange. No es asesoramiento financiero.

## Instalar en Home Assistant

1. Ajustes → Complementos → Tienda de complementos → ⋮ → **Repositorios**.
2. Añade `https://github.com/iejayu8/granmago-scanner` y espera a que recargue.
3. Instala **GranMago Scanner**, arráncalo y activa "Iniciar con Home Assistant" si quieres que vuelva solo tras un reinicio.
4. Abre el panel **GranMago** en la barra lateral (o `http://<tu-home-assistant>:8787`).
5. Pulsa **Iniciar bot** una vez. A partir de ahí sigue solo.

El puerto 8787 no choca con HADomotics (8099).

## Qué hace

- Vigila cada posición abierta cada 20 segundos con el precio en vivo. El stop, el objetivo y el trailing de una moneda no mueven los de otra.
- Escanea los pares de la opción `pairs` cada 90 segundos (configurable), también con posiciones abiertas. El defecto sigue siendo BTC, ETH, SOL, XRP, BNB, ADA, DOGE, AVAX, LINK, DOT, LTC y BCH.
- En cada escaneo automático abre las monedas que llegan al umbral (100 por defecto) y aún no tienen posición, la de mejor score primero, hasta `max_positions` (5 por defecto). Varias pueden abrirse en el mismo escaneo. Nunca dos veces el mismo par, ni en el sentido contrario.
- Un escaneo manual actualiza el ranking y no abre operaciones.
- Con el trailing activo (por defecto), cuando el beneficio no realizado de **esa** posición llega al 1% de su nocional se quitan su TP y su SL iniciales. A partir de ahí solo la cierra su trailing, que se aprieta desde su mejor precio y no deja acabar en pérdida, o su botón **Cerrar**. `trailing_activation_r` por encima de 0 cambia esa activación a un múltiplo del riesgo inicial. `trailing_keep_tp` deja el objetivo puesto.
- Con `trailing_enabled` en false el stop y el objetivo de cada posición siguen fijos.
- Guarda el estado en `/data/state.json` del addon. Un reinicio no borra el balance ni las operaciones, y si estaba en marcha se reanuda solo. Un estado de 1.2.0 con una sola `openPosition` pasa a la lista al cargar: no se cierra y no se le cambia la entrada. Si faltan campos de trailing, se rellenan.

## Riesgo con varias posiciones

Cada entrada arriesga `risk_pct` del **balance realizado** en ese momento, igual que antes. El tamaño no se reparte entre las posiciones: cinco entradas pueden arriesgar 5 × `risk_pct`. Con el 2% por defecto, el riesgo a stop de un libro lleno es el 10% del balance. El valor por defecto de `risk_pct` no se ha bajado.

El apalancamiento no multiplica el nocional. El nocional sale del riesgo y de la distancia al stop. Con el stop mínimo del scanner (1,2% del precio) una operación mueve unas 1,67 veces el balance. Cinco así sumarían unas 8,3 veces el balance, por encima de balance × apalancamiento (3 en la configuración del complemento, o el valor que pongas en la página).

Por eso una **entrada nueva** se reduce al hueco que quede, o no se abre, cuando la suma de nocionales pasaría de balance × apalancamiento. Las que ya están abiertas, incluida una posición migrada de 1.2.0, no se recortan ni se cierran por ese tope. El mejor score del escaneo se lleva el tamaño completo; las siguientes, el resto.

## Ajustes del complemento

| Opción | Por defecto | Uso |
|---|---|---|
| risk_pct | 2 | % del balance arriesgado por operación |
| leverage | 3 | Se guarda en la operación (el tamaño sale del riesgo y del stop) |
| initial_balance | 10000 | Balance inicial de paper, solo al resetear |
| scan_seconds | 90 | Segundos entre escaneos completos (también con posición abierta) |
| trailing_enabled | true | Trailing stop. En false, el SL y el TP no se mueven |
| trailing_activation_pct | 1.0 | % del nocional de beneficio para armar el trailing |
| trailing_distance | 0.5 | Fracción del riesgo inicial (|entrada − SL inicial|) que se deja desde el mejor precio |
| entry_threshold | 100 | Puntuación mínima para abrir. 100 exige también el bonus de estocástico |
| max_positions | 5 | Posiciones abiertas a la vez, una por moneda. De 1 a 12 |
| fee_pct_per_side | 0 | Comisión taker, en % del nocional, en la entrada y en la salida |
| slippage_pct_per_side | 0 | Empeora la entrada y la salida, en % del precio |
| take_profit_r | 1.7 | Objetivo en múltiplos de la distancia al stop inicial. 0 lo quita |
| partial_tp_r | 0 | Cierra `partial_tp_fraction` al llegar a este R. 0 lo desactiva |
| partial_tp_fraction | 0.5 | Fracción que cierra el parcial |
| partial_move_breakeven | true | Tras el parcial, el stop pasa a la entrada si eso lo aprieta |
| stop_mode | swing | `swing` (10 velas de 4H) o `atr` |
| stop_atr_period | 14 | Periodo del ATR de 4H |
| stop_atr_mult | 2 | Distancia del stop en ATR cuando `stop_mode` es `atr` |
| trailing_activation_r | 0 | Activa el trailing en R. 0 usa `trailing_activation_pct` |
| trailing_keep_tp | false | Si es true, el trailing no cancela el objetivo |
| pair_cooldown_minutes | 0 | Espera tras cerrar un par antes de volver a abrirlo |
| max_same_direction_risk_r | 0 | Tope de riesgo inicial abierto en largos, y otro igual en cortos, en unidades de una operación. 0 lo desactiva |
| regime_filter | off | `off`, `slope`, `adx`, `er`, `slope_adx` o `slope_er` |
| regime_adx_min | 20 | ADX mínimo de 4H cuando el filtro usa adx |
| regime_er_min | 0.2 | Efficiency ratio mínimo de 4H cuando el filtro usa er |
| strategy | scanner | `scanner` (score de 1.4.0) o `granmago` (reglas del curso) |
| granmago_variant | a | `a`: largos y cortos, dos cierres de 4H. `b`: solo largos, un cierre de 12H |
| granmago_max_adds | 0 | Añadidos en el extremo barrido. 0, 1 o 2. 0 no promedia |
| granmago_momentum_exit | true | Cierra si el estocástico y el squeeze se agotan |
| granmago_max_hold_bars | 15 | Velas de confirmación antes de salir por tiempo. 0 lo quita |
| granmago_breakeven_r | 0 | Mueve el stop a la entrada al llegar a este R. 0 no lo mueve |
| granmago_min_rr | 0 | Relación mínima objetivo/stop. 0 acepta un stop más ancho que el objetivo |
| granmago_stop_pad_pct | 0.3 | Margen mínimo del stop más allá de la mecha, en % del precio |
| granmago_style | strict | `strict` (filtros originales) o `sweep` (barrido relajado) |
| granmago_setups | both | `both`, `sweep` (solo setup A) o `pullback` (solo setup B) |
| pairs | los 12 de siempre | Swaps USDT a escanear. Vacío o inválido vuelve a esos 12. Máximo 40 |
| log_level | info | Log del complemento |

## Riesgo, apalancamiento y el estado guardado

`risk_pct` y `leverage` del complemento se exportan al arrancar y **reemplazan** `riskPct` y `leverage` de `/data/state.json`. Hasta la 1.3.0 el estado ganaba, así que cambiar la opción no cambiaba el tamaño de las entradas si la página ya había guardado otro valor.

La página sigue pudiendo cambiar el riesgo y el apalancamiento mientras el complemento está en marcha. Esos cambios se guardan, y el siguiente arranque vuelve a aplicar las opciones del complemento. El resto de opciones (umbral, trailing, costes, stop, régimen, cooldown) funcionan igual: la opción del complemento manda al arrancar.

El apalancamiento no multiplica el nocional de una entrada. Limita la suma de nocionales a balance × apalancamiento. El nocional de cada entrada sale del riesgo y de la distancia al stop.

## Costes, R y régimen

Con comisión y slippage en 0 el paper se comporta como en 1.3.0. Para acercarlo a una orden taker de Blofin, `fee_pct_per_side` 0,06 y `slippage_pct_per_side` 0,02. La comisión de entrada sale del balance al abrir. La de salida, y el slippage de ambos lados, entran en el PnL del historial. `rMultiple` es ese PnL neto partido por el riesgo inicial de esa parte. Si hubo un parcial, la fila del cierre final lleva `tradePnl` y `tradeR` de la operación entera.

El score 100 ya no es el mismo conjunto que 95. 100 exige el bonus de estocástico (largo con %K < 60, corto con %K > 40). 95 es el setup completo sin ese bonus. No hay puntuaciones entre 81 y 94, así que un umbral de 90 abre lo mismo que 95.

`regime_filter` en `off` no descarta señales. `slope` mira la EMA50 diaria contra su valor de hace 5 días: no abre cortos si esa media sube ni largos si baja. `adx` exige ADX(14) de 4H ≥ `regime_adx_min`. `er` exige el efficiency ratio de Kaufman de 20 velas de 4H ≥ `regime_er_min`. Las combinaciones exigen las dos cosas. Si falta el dato, esa entrada no se abre. Con `strategy: granmago` ese filtro sigue aplicando si se enciende; la tendencia del curso es otra y no depende de él.

## Modo Gran Mago

`strategy: scanner` es el defecto y no cambia las entradas de 1.4.0. `strategy: granmago` deja de abrir por score. El escaneo sigue calculando el score para la tabla, pero la orden sale de las reglas del curso y de lo que él publica en X.

La variante `a` opera los dos lados y espera **dos cierres de 4H**. La variante `b` solo abre largos y espera **un cierre de 12H**. La vela de 12H son tres velas de 4H de Blofin: 00, 04 y 08 UTC, o 12, 16 y 20 UTC. No se entra a mitad de vela ni en una vela posterior si la de confirmación ya pasó.

La fase la marcan la EMA50 semanal y la diaria. Si no coinciden, manda la diaria. Un solo cierre al otro lado de la media no cambia la fase; hacen falta dos seguidos.

El setup A barre el mínimo o máximo diario importante (pivote de cierres, cinco velas a cada lado, el más extremo de los últimos 30 días que aún no se haya barrido) y espera los cierres de vuelta al nivel donde más mínimos o máximos de la vela de confirmación coinciden, con una tolerancia del 0,2 % y al menos tres toques. Si no hay ese racimo, el nivel es el propio pivote. La ventana es de 30 días porque 90 dejaba sin barrer el origen de la tendencia y ese mínimo viejo impedía cualquier barrido más cercano: el extremo que cuenta es el del rango que se ve. El setup B es el retroceso a favor de tendencia: la mecha toca la EMA50 diaria, un swing ya roto, un imbalance de tres velas o el 50 % de una mecha grande, y luego vienen los mismos cierres. Mientras el setup A está armado no se toma el B.

El estocástico es 14, 3, 3 y solo veta: no hay largo con %K por encima de 80 ni corto con %K por debajo de 20. La zona media no bloquea. El filtro de impulso aproxima el script «SQZ ADX» de TradingView, que no publica parámetros, con el Squeeze Momentum de LazyBear: Bandas de Bollinger 20 y 2, Canal de Keltner 20 y 1,5 veces la media simple del true range, histograma = regresión lineal de `cierre − media(media(máximo 20, mínimo 20), media del cierre 20)`. Para un largo hace falta el histograma negativo y subiendo, o cruzando por encima de cero, y el ADX(14) de Wilder subiendo respecto a la vela anterior (`adx > adx[1]`). El corto es el espejo. Esos indicadores se miran en la vela de confirmación (4H o 12H), no en diario.

El stop es un stop de precio más allá de la mecha barrida, a `max(0,5 × ATR de 14 velas de 4H, granmago_stop_pad_pct % del precio)`. Si un imbalance sin rellenar o una mecha grande atraviesa ese margen, el stop pasa al otro lado del hueco. Por encima del 8 % del precio la operación no se abre. En X la invalidación a veces es el cierre de 12H al otro lado de la mecha: aquí ese cierre solo impide promediar; la posición espera el stop de precio, porque el bot vigila cada tick. El objetivo es el clúster del swing diario anterior en la dirección del beneficio, un 0,1 % por dentro del extremo. No hay objetivo en R. `granmago_min_rr` en 0 no descarta un stop más ancho que el objetivo. Sus propias cifras (77 % de aciertos y profit factor 1,38) implican un beneficio medio de unas 0,4 veces la pérdida media, así que el filtro de 1,5 del apunte del curso queda apagado a propósito.

No hay trailing en este modo. `granmago_momentum_exit` cierra cuando el estocástico está agotado y el histograma del squeeze pierde fuerza. A las `granmago_max_hold_bars` velas de confirmación (15 por defecto) se cierra por tiempo. `granmago_breakeven_r` en 0 no mueve el stop: él lo mencionó y no llegó a explicarlo.

`pairs` es la lista que recorre el escaneo. El defecto, y lo que hace una actualización si no se toca la opción, son los 12 pares de arriba. Los 28 del walk-forward largo son esos 12 más ZEC, PEPE, 1000BONK, NEAR, XMR, INJ, SUI, TAO, IOST, XLM, TRX, HBAR, ENA, WLD, AAVE y QNT. No es el defecto. Un símbolo que no sea `BASE-USDT` se ignora. Si no queda ninguno válido se usan los 12. El tope es 40. `max_positions` sigue siendo el número de huecos (de 1 a 12), no el tamaño del universo. Quitar un par de la lista no cierra una posición que ya esté abierta: el bucle de 20 segundos la sigue vigilando.

`granmago_style: sweep` cambia el setup A. El nivel barrido es el más extremo entre un pivote de cierre diario de los últimos 20 días (sigue siendo 5 y 5), un pivote de cierre de 4H de los últimos 30 bars de 4H, y el máximo o mínimo del día anterior. La confirmación es el cierre de vuelta al otro lado de ese precio, en las mismas velas de la variante. Una vela que no recupera el nivel cancela el barrido; no se espera a una vela posterior. La tendencia es el último cierre diario contra la EMA50, sin semanal y sin las dos velas de histéresis. El estocástico deja de vetar y pasa a confirmar: el largo necesita un cruce de %K por encima de 20 entre el barrido y la señal, y el corto un cruce por debajo de 80. El squeeze y el ADX se guardan en la señal (`sqzOk`, `adxOk`) y no impiden la entrada. El stop, el objetivo y el tope del 8 % no cambian. `granmago_setups: sweep` deja fuera el retroceso; `pullback` deja fuera el barrido. El defecto del complemento sigue siendo `strict` y `both`.

`granmago_max_adds` en 0 no promedia. En 1 o 2, la primera orden arriesga `risk_pct / (1 + añadidos)` y cada añadido el mismo importe, así el stop de todas las patas sigue siendo un solo `risk_pct`. El añadido solo entra si el precio toca el extremo barrido sin tocar el stop, y nunca después de un cierre de confirmación al otro lado de ese extremo.

El riesgo del 2 % y el apalancamiento 3 ya eran el defecto del complemento y coinciden con lo que él escribe en X («máx 2 % y x3»). El filtro de noticias no está: el curso dice no operar con datos y en X dice que el IPC no le cambia la idea, y no hay un calendario fiable en el complemento. La contratendencia y el scalping de varios intentos tampoco se programan.
