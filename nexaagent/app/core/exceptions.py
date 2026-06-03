class UpstreamUnavailable(Exception):
    """El servicio de inferencia (Ollama) no respondió. Se traduce a 503."""