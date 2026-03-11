import urllib.parse

import requests
from bs4 import BeautifulSoup
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, JSONResponse
from pydantic import BaseModel
from supabase import create_client, Client
import os
import socket
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env")) # Load from root .env

def is_safe_url(url: str) -> bool:
    """Check if the URL is safe to fetch (prevents SSRF)."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        # Prevent internal IP access
        ip = socket.gethostbyname(hostname)
        # Check for loopback, private, and link-local ranges
        if ip.startswith(("127.", "10.", "172.16.", "192.168.", "169.254.")):
            return False
        return True
    except:
        return False

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
HF_TOKEN = os.environ.get("HF_TOKEN", "").strip()

def get_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(status_code=500, detail="Supabase configuration missing. Please add SUPABASE_URL and SUPABASE_KEY to your Vercel Environment Variables.")
    return create_client(SUPABASE_URL, SUPABASE_KEY)

class IndexRequest(BaseModel):
    url: str


class SearchRequest(BaseModel):
    query: str


class SearchResponse(BaseModel):
    label: str
    url: str


# Module-level state — one indexed site at a time
_embeddings = None
_indexed_url = None


class HFEmbedder:
    def __init__(self, token: str):
        self.api_url = "https://router.huggingface.co/hf-inference/models/sentence-transformers/all-MiniLM-L6-v2"
        self.headers = {"Authorization": f"Bearer {token}"}

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        all_embeddings = []
        batch_size = 50
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i+batch_size]
            response = requests.post(self.api_url, headers=self.headers, json={"inputs": batch, "options": {"wait_for_model": True}}, timeout=60)
            if response.status_code != 200:
                raise Exception(f"HF API Error: {response.text}")
            embeddings = response.json()
            # The API returns a list of embeddings for the batch
            if isinstance(embeddings, list):
                all_embeddings.extend(embeddings)
            else:
                raise Exception("Unexpected HF API response format")
        return all_embeddings

    def embed_query(self, text: str) -> list[float]:
        response = requests.post(self.api_url, headers=self.headers, json={"inputs": [text], "options": {"wait_for_model": True}}, timeout=60)
        if response.status_code != 200:
            raise Exception(f"HF API Error: {response.text}")
        embeddings = response.json()
        return embeddings[0]


def get_embeddings():
    global _embeddings
    if _embeddings is None:
        _embeddings = HFEmbedder(HF_TOKEN)
    return _embeddings


app = FastAPI(title="AI Navigator API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    return JSONResponse(
        status_code=500,
        content={
            "detail": str(exc),
            "type": str(type(exc).__name__),
            "traceback": traceback.format_exc() if os.environ.get("VERCEL") else None
        }
    )

@app.get("/api/status")
def status():
    config_missing = []
    if not SUPABASE_URL: config_missing.append("SUPABASE_URL")
    if not SUPABASE_KEY: config_missing.append("SUPABASE_KEY")
    if not HF_TOKEN: config_missing.append("HF_TOKEN")
    
    if config_missing:
        return {
            "ready": False, 
            "error": "Configuration Missing", 
            "missing_vars": config_missing,
            "message": f"Please add the following variables to Vercel: {', '.join(config_missing)}"
        }

    if _indexed_url is None:
        return {"ready": False, "message": "No website indexed recently.", "indexed_url": None}
    return {"ready": True, "message": "Website indexed and ready.", "indexed_url": _indexed_url}


@app.post("/api/index")
def index_website(payload: IndexRequest):
    global _indexed_url

    base_url = payload.url.strip()
    if not is_safe_url(base_url):
        raise HTTPException(status_code=400, detail="Invalid or unsafe URL. Only public http/https are allowed.")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/91.0.4472.124 Safari/537.36"
        )
    }

    try:
        response = requests.get(base_url, headers=headers, timeout=15, verify=False)
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not reach website: {exc}") from exc

    soup = BeautifulSoup(response.text, "html.parser")
    
    texts = []
    hrefs = []
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(strip=True)
        href = anchor["href"]
        full_url = urllib.parse.urljoin(base_url, href)
        if text and len(text) > 2 and full_url and not full_url.startswith('javascript:'):
            texts.append(text)
            hrefs.append(full_url)

    if not texts:
        raise HTTPException(status_code=400, detail="No links found on the target site.")

    # 1. Clear old data from Supabase so we only search the current site
    try:
        sb = get_supabase()
        sb.table("site_links").delete().neq("id", 0).execute()
    except HTTPException:
        raise
    except Exception as e:
        print(f"Warning: Could not clear old links. Error: {e}")
        pass 

    # 2. Embed all texts
    if not HF_TOKEN:
        raise HTTPException(status_code=500, detail="HF_TOKEN environment variable is missing on Vercel.")
    
    embedder = get_embeddings()
    vectors = embedder.embed_documents(texts)
    
    # 3. Prepare records for Supabase
    records = []
    for i in range(len(texts)):
        records.append({
            "url": hrefs[i],
            "label": texts[i],
            "embedding": vectors[i]
        })
        
    # 4. Insert into Supabase in batches of 100 to avoid request size limits
    batch_size = 100
    try:
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            sb.table("site_links").insert(batch).execute()
    except Exception as e:
         raise HTTPException(status_code=500, detail=f"Database error during insert: {e}")

    _indexed_url = base_url

    return {"success": True, "indexed_url": _indexed_url, "links_count": len(records)}


@app.post("/api/search", response_model=SearchResponse)
def search(payload: SearchRequest):
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query cannot be empty.")

    embedder = get_embeddings()
    query_vector = embedder.embed_query(query)

    try:
        sb = get_supabase()
        response = sb.rpc(
            "match_links", 
            {"query_embedding": query_vector, "match_threshold": 0.3, "match_count": 1}
        ).execute()
        results = response.data
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database search failed: {exc}") from exc

    if not results or len(results) == 0:
        raise HTTPException(status_code=404, detail="No matching link found.")

    match = results[0]
    return SearchResponse(label=match["label"], url=match["url"])


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )
}

# Headers that block embedding — we strip these before forwarding to the browser
_BLOCKED_HEADERS = {
    "x-frame-options",
    "content-security-policy",
    "content-security-policy-report-only",
    "x-content-type-options",
    "content-length",
    "content-encoding",
}


@app.api_route("/api/proxy", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy(request: Request, url: str = Query(..., description="Full URL to proxy")):
    """Fetch a remote URL and return it without iframe-blocking headers."""
    if not is_safe_url(url):
        raise HTTPException(status_code=400, detail="Invalid or unsafe URL. Only public http/https are allowed.")

    try:
        body = await request.body()
        # Use streaming to prevent downloading massive malicious files into memory
        MAX_SIZE = 10 * 1024 * 1024 # 10MB limit
        with requests.request(
            method=request.method,
            url=url,
            headers=HEADERS,
            data=body,
            timeout=15,
            stream=True,
            verify=False
        ) as resp:
            # Check content-length header if present
            cl = resp.headers.get("content-length")
            if cl and int(cl) > MAX_SIZE:
                raise HTTPException(status_code=413, detail="Payload too large (>10MB).")
            
            # Check actual content size as it comes in
            content = b""
            for chunk in resp.iter_content(chunk_size=8192):
                content += chunk
                if len(content) > MAX_SIZE:
                    raise HTTPException(status_code=413, detail="Payload too large (>10MB).")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch URL: {exc}") from exc

    content_type = resp.headers.get("content-type", "text/html")

    # For HTML pages: inject <base> tag so relative URLs resolve on the real site,
    # and inject a script to intercept link clicks and forward them to the parent frame.
    if "text/html" in content_type:
        # Inject link-interceptor and network-interceptor
        html_content = resp.content.decode("utf-8", errors="replace")
        
        # Neutralize common frame-busting patterns
        # Replace 'top.location = self.location' and similar with a no-op
        import re
        # Even more aggressive neutralization
        html_content = re.sub(r'(window\.)?(top|parent)\.(location|location\.href|location\.replace|location\.assign)\b\s*=?\s*', r'/* blocked */ \1\2._blocked_loc =', html_content, flags=re.I)
        html_content = re.sub(r'(window\.)?(top|parent)\[[\'"]location[\'"]\]\s*=', r'/* blocked */ \1\2["_blocked_loc"] =', html_content, flags=re.I)
        html_content = re.sub(r'if\s*\((window\.)?(top|parent)\s*!==?\s*(window\.)?self\)', r'if(false)', html_content, flags=re.I)
        html_content = re.sub(r'if\s*\((window\.)?self\s*!==?\s*(window\.)?(top|parent)\)', r'if(false)', html_content, flags=re.I)

        soup = BeautifulSoup(html_content, "html.parser")
        if not soup.find("base"):
            base_tag = soup.new_tag("base", href=url)
            if soup.head:
                soup.head.insert(0, base_tag)
            elif soup.html:
                head = soup.new_tag("head")
                head.append(base_tag)
                soup.html.insert(0, head)
        
        # Inject ultra-robust anti-frame-buster at the VERY top of head if possible
        spoof_script = soup.new_tag("script")
        spoof_script.string = """
(function() {
  // Spoof window hierarchy
  try {
    Object.defineProperty(window, 'top', { get: function() { return window; }, configurable: false });
    Object.defineProperty(window, 'parent', { get: function() { return window; }, configurable: false });
    window.self = window;
    window.frameElement = null;
  } catch (e) {}

  // DOM-Lock: Prevent scripts from hiding the body or document
  const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
      if (mutation.type === 'attributes' && (mutation.attributeName === 'style' || mutation.attributeName === 'hidden')) {
        const target = mutation.target;
        if (target === document.body || target === document.documentElement) {
          if (target.style.display === 'none' || target.style.visibility === 'hidden' || target.hidden) {
            target.style.display = 'block';
            target.style.visibility = 'visible';
            target.hidden = false;
          }
        }
      }
    });
  });
  observer.observe(document.documentElement, { attributes: true, subtree: true });
  
  // Intercept attempts to clear the body
  const originalClear = document.write;
  document.write = function(h) { if (h.length > 10) originalClear.apply(document, arguments); };
})();
"""
        if soup.head:
            soup.head.insert(0, spoof_script)
        elif soup.html:
            soup.html.insert(0, spoof_script)

        # Inject link-interceptor and network-interceptor
        interceptor = soup.new_tag("script")
        interceptor.string = """
(function () {
  const BASE_URL = '""" + url + """';
  const PROXY_ROOT = window.location.origin + '/api/proxy?url=';

  function resolve(url) {
    if (!url) return url;
    if (url.startsWith('data:') || url.startsWith('blob:') || url.startsWith('javascript:')) return url;
    
    // Handle protocol-relative URLs manually just in case
    if (url.startsWith('//')) {
      return (BASE_URL.startsWith('https:') ? 'https:' : 'http:') + url;
    }

    try {
      return new URL(url, BASE_URL).href;
    } catch (_) {
      return url;
    }
  }

  function proxify(url) {
    const resolved = resolve(url);
    if (!resolved || resolved.startsWith('data:') || resolved.startsWith('blob:') || resolved.startsWith('javascript:')) {
       return url;
    }
    // Don't double proxy
    if (resolved.startsWith(window.location.origin + '/api/proxy')) return resolved;
    return PROXY_ROOT + encodeURIComponent(resolved);
  }

  // Intercept Link Clicks
  function interceptLinks() {
    document.querySelectorAll('a[href]').forEach(function (a) {
      if (a._pi) return;
      a._pi = true;
      a.addEventListener('click', function (e) {
        var href = a.getAttribute('href');
        if (!href || href.startsWith('#') || href.startsWith('mailto:') || href.startsWith('tel:') || href.startsWith('javascript:')) return;
        e.preventDefault();
        e.stopPropagation();
        window.parent.postMessage({ type: 'PROXY_NAVIGATE', url: resolve(href) }, '*');
      }, true);
    });
  }

  // Monkey-patch Fetch
  const originalFetch = window.fetch;
  window.fetch = function(input, init) {
    if (typeof input === 'string') {
      input = proxify(input);
    } else if (input instanceof Request) {
      const newUrl = proxify(input.url);
      input = new Request(newUrl, input);
    }
    return originalFetch(input, init);
  };

  // Monkey-patch XHR
  const originalOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...args) {
    return originalOpen.apply(this, [method, proxify(url), ...args]);
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', interceptLinks);
  } else {
    interceptLinks();
  }
  new MutationObserver(interceptLinks).observe(document.documentElement, { childList: true, subtree: true });
})();
"""
        if soup.body:
            soup.body.append(interceptor)
        elif soup.html:
            soup.html.append(interceptor)

        body = str(soup).encode("utf-8", errors="replace")
        return Response(
            content=body,
            media_type="text/html; charset=utf-8",
        )

    # Non-HTML (images, CSS, JS, fonts…) — pass through as-is
    forward_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in _BLOCKED_HEADERS
        and k.lower() not in ("transfer-encoding", "connection")
    }
    return Response(content=resp.content, media_type=content_type, headers=forward_headers)
