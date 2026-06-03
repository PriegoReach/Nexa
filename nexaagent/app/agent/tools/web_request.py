import httpx
from langchain_core.tools import tool


@tool
def http_get(url: str) -> str:
    """Fetch the contents of a public URL via HTTP GET.

    Use this to call external read-only APIs or fetch public web pages.
    """
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        return resp.text[:4000]
    except httpx.HTTPError as exc:
        return f"Request failed: {exc}"
