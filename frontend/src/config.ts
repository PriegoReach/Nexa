// Única fuente de la URL base de la API. Se lee de VITE_API_URL (.env) con un
// fallback al puerto local por defecto. No repartir esta cadena por el código.
export const API_URL: string =
  import.meta.env.VITE_API_URL ?? "http://localhost:8000";
