# GranMago Scanner

Complemento de Home Assistant. Paper trading del scanner GranMago (EMA50 semanal / diario / 4H, estocástico y zona de liquidez).

El bucle corre **dentro del servidor**. Puedes cerrar la página o apagar la pantalla del móvil: la posición, el balance y el historial siguen en el addon.

No envía órdenes a ningún exchange. No es asesoramiento financiero.

## Instalar en Home Assistant

1. Ajustes → Complementos → Tienda de complementos → ⋮ → **Repositorios**.
2. Añade `https://github.com/iejayu8/granmago-scanner` y espera a que recargue.
3. Instala **GranMago Scanner**, arráncalo y activa "Iniciar con Home Assistant" si quieres que vuelva solo tras un reinicio.
4. Abre el panel **GranMago** en la barra lateral (o `http://<tu-home-assistant>:8787`).
5. Pulsa **Iniciar bot** una vez. A partir de ahí sigue solo.

El puerto 8787 no choca con HADomotics (8099).

## Qué hace

- Vigila una posición abierta cada 20 segundos con el precio en vivo (stop, objetivo y, si toca, trailing).
- Escanea BTC, ETH, SOL, XRP, BNB, ADA, DOGE, AVAX, LINK, DOT, LTC y BCH cada 90 segundos (configurable), también con una posición abierta. Los scores se actualizan, pero no abre una segunda operación.
- Entra en paper solo si no hay posición, el score llega al umbral de entrada (100 por defecto, el máximo) y el sesgo semanal acompaña.
- Con el trailing activo (por defecto), cuando el beneficio no realizado llega al 1% del nocional se quitan el TP y el SL iniciales. A partir de ahí solo cierra el trailing, que se aprieta desde el mejor precio y no deja acabar en pérdida, o **Cerrar posición**.
- Con `trailing_enabled` en false el stop y el objetivo siguen fijos, como en 1.0.0.
- Guarda el estado en `/data/state.json` del addon. Un reinicio del complemento no borra el balance ni la operación abierta, y si estaba en marcha se reanuda solo. Una posición guardada sin campos de trailing se rellena y sigue abierta.

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
| log_level | info | Log del complemento |
