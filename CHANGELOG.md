# Changelog

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
