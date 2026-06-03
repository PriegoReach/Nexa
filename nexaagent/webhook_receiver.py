"""Receptor de webhooks de PRUEBA/DEMO (P22 Bloque B) — servicio en compose.

NO es producción: es el receptor contra el que se verifica call_webhook, formalizado
en docker-compose.yml (servicio webhook-receiver) para que sea reproducible y sobreviva
a compose down/up — en vez de un `docker run` suelto (un "fósil" de P15). La URL de
producción real es un pendiente: se cambia en el secreto webhook_url.txt, no en el código.

Loguea cada POST recibido a stdout (-> docker logs) para contar cuántos llegan REALMENTE
al receptor: evidencia del efecto, no de la intención.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        print(f"CAPTURE POST path={self.path} body={body}", flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):
        return


if __name__ == "__main__":
    print("webhook-receiver listening on :9000", flush=True)
    HTTPServer(("0.0.0.0", 9000), H).serve_forever()
