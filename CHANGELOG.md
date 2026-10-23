# Changelog

## 1.4.0

- Comisión taker y slippage en el paper y en el backtest (`fee_pct_per_side`, `slippage_pct_per_side`). Por defecto 0, así que el balance de una instalación actual no cambia hasta que se pongan. El taker de Blofin ronda 0,06 % y un slippage de 0,02 % por lado es el supuesto usado en la evaluación.
- El trailing puede armarse en múltiplos de R (`trailing_activation_r`). Con 0 se mantiene la activación en porcentaje del nocional. `trailing_keep_tp` deja el objetivo puesto al armarse; por defecto se sigue quitando.
- Objetivo configurable (`take_profit_r`, 1,7) y parcial opcional (`partial_tp_r` en 0, `partial_tp_fraction` 0,5). Tras el parcial el stop puede pasar a breakeven.
- Stop por ATR(4H) opcional (`stop_mode`: `swing` o `atr`). El defecto sigue siendo el swing de 10 velas, con el mismo suelo del 1,2 % y el mismo tope del 8 %.
- Enfriamiento por par (`pair_cooldown_minutes`, 0) y tope de riesgo abierto en la misma dirección (`max_same_direction_risk_r`, 0). El tope cuenta el riesgo inicial de cada posición hasta que se cierra del todo.
- El historial guarda `initialSl`, `riskAmt`, `initialRiskAmt`, `rMultiple` y, si hubo parcial, `tradePnl` y `tradeR` de la operación completa.
- El score ya no recorta 105 a 100. El sesgo pasa de 35 a 30 puntos para que el bonus de estocástico quepa en 100. 100 exige el bonus; 95 es el setup completo sin él. 90 sigue coincidiendo con 95, porque el siguiente score real es 80.
- Filtro de régimen opcional (`regime_filter`: `off`, `slope`, `adx`, `er`, `slope_adx`, `slope_er`). `off` no cambia las entradas. `slope` usa la pendiente de la EMA50 diaria (5 velas): no abre largos si baja ni cortos si sube. ADX(14) y el efficiency ratio de Kaufman (20 velas de 4H) exigen un mínimo (`regime_adx_min` 20, `regime_er_min` 0,2).
- `risk_pct` y `leverage` del complemento se aplican en cada arranque y pisan los valores guardados en `state.json`. La página puede cambiarlos hasta el siguiente reinicio.

## 1.3.0

- Hasta `max_positions` operaciones abiertas a la vez (entero, por defecto 5, de 1 a 12). Cada una va en una moneda distinta: no se repite el par ni se cubre el mismo activo en largo y corto.
- En cada escaneo automático se rellenan los huecos libres con los pares que llegan a `entry_threshold`, el mejor score primero. Varios pares del mismo escaneo pueden abrirse. Un escaneo manual no abre operaciones.
- Cada posición lleva su propio SL, TP y trailing (activación sobre su nocional, distancia sobre su riesgo inicial, breakeven, solo se aprieta). Los motivos de cierre siguen siendo SL, TP, trailing y manual.
- El riesgo de cada entrada sigue siendo `risk_pct` del balance realizado en ese momento. Con 5 posiciones el riesgo a stop puede llegar a 5 × `risk_pct`. El 2% por defecto no cambia.
- El nocional nuevo se recorta, o no se abre, si la suma de nocionales pasaría de balance × apalancamiento. Una posición ya abierta no se recorta ni se cierra por ese tope.
- La página muestra la tabla de posiciones, el PnL no realizado, el equity y «x/5 posiciones». `POST /api/close_position` recibe `{"pair": "ETH-USDT"}`.
- Un `state.json` de 1.2.0 con `openPosition` (la short de ETH-USDT incluida) pasa a la lista al arrancar, sin cerrarla. Balance e historial se conservan.

## 1.2.0

- La entrada automática exige el score máximo: opción `entry_threshold` (entero, por defecto 100, de 0 a 100). Con el valor por defecto solo abre si el score es 100.
- El ranking marca LONG/SHORT solo cuando el score llega a ese umbral. El escaneo, una sola posición, el cierre manual y el trailing no cambian.

## 1.1.0

- El escaneo automático sigue cada intervalo aunque haya una posición abierta. Actualiza las señales y no abre una segunda posición.
- Botón **Cerrar posición** y `POST /api/close_position`: cierra a mercado, motivo `manual`, mismo cálculo de balance que un SL/TP.
- Trailing stop (activo por defecto). Se arma cuando el PnL no realizado llega a `trailing_activation_pct` (1% del nocional). Entonces desaparecen el TP y el SL iniciales y solo puede cerrar el trailing, con motivo `trailing`, o un cierre manual. La distancia es `trailing_distance` (0.5 del riesgo inicial) desde el mejor precio. Al activarse el stop queda como mínimo en el breakeven y después solo se aprieta.
- `trailing_enabled` en false deja el SL y el TP fijos.
- No hay cierre por tiempo. Un estado de 1.0.0 con posición abierta se carga, se rellenan los campos que falten y la operación sigue.

## 1.0.0

- Versión inicial del complemento.
