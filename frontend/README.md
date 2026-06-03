# Nexa — Frontend (MVP, Fase 3 / P31)

Interfaz web de NexaAgent. Proyecto **separado** del backend: React + Vite +
TypeScript. Se comunica con la API REST (FastAPI) solo por HTTP/JSON.

## Requisitos
- Node 18+ (probado con Node 22).
- El backend corriendo en `http://localhost:8000` (Docker: `docker compose up`).

## Desarrollo

```bash
npm install
npm run dev
```

Abre **http://localhost:5173** (puerto fijo; el backend tiene CORS atado a él).

## Configuración
La URL de la API se lee de `VITE_API_URL` (ver `.env`). Por defecto
`http://localhost:8000`.

## Alcance del MVP
Login (`POST /auth/login`) + chat síncrono (`POST /chat`). El JWT vive en el
estado de React (no `localStorage`); se pierde al recargar, a propósito.
Aún no: streaming, confirmación de acciones, historial de conversaciones,
OAuth desde la UI, subida de documentos.
