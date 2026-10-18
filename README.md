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
- Con el trailing activo (por defecto), cuando el beneficio no realizado de **esa** posición llega al 1% de su nocional se quitan su TP y su SL iniciales. A partir de ahí solo la cierra su trailing, que se aprieta desde su mejor precio y no deja acabar en pérdida, o su botón **Cerrar**.
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
| entry_threshold | 100 | Puntuación mínima para abrir. 100 es el máximo del scanner |
| max_positions | 5 | Posiciones abiertas a la vez, una por moneda. De 1 a 12 |
| log_level | info | Log del complemento |
