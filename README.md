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
- Escanea BTC, ETH, SOL, XRP, BNB, ADA, DOGE, AVAX, LINK, DOT, LTC y BCH cada 90 segundos (configurable), también con posiciones abiertas.
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
| log_level | info | Log del complemento |

## Riesgo, apalancamiento y el estado guardado

`risk_pct` y `leverage` del complemento se exportan al arrancar y **reemplazan** `riskPct` y `leverage` de `/data/state.json`. Hasta la 1.3.0 el estado ganaba, así que cambiar la opción no cambiaba el tamaño de las entradas si la página ya había guardado otro valor.

La página sigue pudiendo cambiar el riesgo y el apalancamiento mientras el complemento está en marcha. Esos cambios se guardan, y el siguiente arranque vuelve a aplicar las opciones del complemento. El resto de opciones (umbral, trailing, costes, stop, régimen, cooldown) funcionan igual: la opción del complemento manda al arrancar.

El apalancamiento no multiplica el nocional de una entrada. Limita la suma de nocionales a balance × apalancamiento. El nocional de cada entrada sale del riesgo y de la distancia al stop.

## Costes, R y régimen

Con comisión y slippage en 0 el paper se comporta como en 1.3.0. Para acercarlo a una orden taker de Blofin, `fee_pct_per_side` 0,06 y `slippage_pct_per_side` 0,02. La comisión de entrada sale del balance al abrir. La de salida, y el slippage de ambos lados, entran en el PnL del historial. `rMultiple` es ese PnL neto partido por el riesgo inicial de esa parte. Si hubo un parcial, la fila del cierre final lleva `tradePnl` y `tradeR` de la operación entera.

El score 100 ya no es el mismo conjunto que 95. 100 exige el bonus de estocástico (largo con %K < 60, corto con %K > 40). 95 es el setup completo sin ese bonus. No hay puntuaciones entre 81 y 94, así que un umbral de 90 abre lo mismo que 95.

`regime_filter` en `off` no descarta señales. `slope` mira la EMA50 diaria contra su valor de hace 5 días: no abre cortos si esa media sube ni largos si baja. `adx` exige ADX(14) de 4H ≥ `regime_adx_min`. `er` exige el efficiency ratio de Kaufman de 20 velas de 4H ≥ `regime_er_min`. Las combinaciones exigen las dos cosas. Si falta el dato, esa entrada no se abre.
