# Changelog

## 1.1.0

- El escaneo automático sigue cada intervalo aunque haya una posición abierta. Actualiza las señales y no abre una segunda posición.
- Botón **Cerrar posición** y `POST /api/close_position`: cierra a mercado, motivo `manual`, mismo cálculo de balance que un SL/TP.
- Nueva opción `max_hold_hours` (por defecto 48; 0 la desactiva). Si se supera, cierra a mercado con motivo `timeout`.
- El estado guardado (posición abierta, historial y balance) se carga igual si faltan los campos nuevos.

## 1.0.0

- Versión inicial del complemento.
