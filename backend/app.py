import urllib.parse
import requests
from bs4 import BeautifulSoup
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, JSONResponse
from pydantic import BaseModel
from supabase import create_client, Client, ClientOptions
import socket
import time
import os
from dotenv import load_dotenv
from huggingface_hub import InferenceClient

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
SUPABASE_URL_2 = os.environ.get("SUPABASE_URL_2", "").strip()
SUPABASE_KEY_2 = os.environ.get("SUPABASE_KEY_2", "").strip()
HF_TOKEN = os.environ.get("HF_TOKEN", "").strip()

_sb_clients = {}

def get_supabase_clients() -> dict:
    global _sb_clients
    if not _sb_clients:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise HTTPException(status_code=500, detail="Supabase configuration missing.")
        
        # Common options to prevent [Errno 16] Busy errors (Forcing stable HTTP/1.1)
        options = ClientOptions(
            postgrest_client_timeout=30,
            storage_client_timeout=30
        )
        
        # Initialize Primary
        _sb_clients["primary"] = create_client(SUPABASE_URL, SUPABASE_KEY, options=options)
        
        # Initialize Secondary (Optional)
        if SUPABASE_URL_2 and SUPABASE_KEY_2:
            try:
                _sb_clients["secondary"] = create_client(SUPABASE_URL_2, SUPABASE_KEY_2, options=options)
            except Exception as e:
                print(f"Warning: Could not initialize secondary Supabase: {e}")
        else:
            print("Notice: Secondary Supabase URL/Key missing. Failover is disabled.")
                
    return _sb_clients

class IndexRequest(BaseModel):
    url: str
    user_id: str


class SearchRequest(BaseModel):
    query: str
    user_id: str


class SearchResponse(BaseModel):
    label: str
    url: str


# Module-level state — one indexed site at a time
_embeddings = None
_indexed_url = None


class HFEmbedder:
    def __init__(self, token: str):
        self.client = InferenceClient(
            model="sentence-transformers/all-MiniLM-L6-v2",
            token=token
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        # InferenceClient.feature_extraction returns a numpy array or list of floats
        # We handle batching or rely on the client's internal handling
        try:
            embeddings = self.client.feature_extraction(texts)
            # Convert to list of lists if it's a 2D numpy-like array
            if hasattr(embeddings, "tolist"):
                return embeddings.tolist()
            return embeddings
        except Exception as e:
            raise Exception(f"HuggingFace Hub Error: {e}")

    def embed_query(self, text: str) -> list[float]:
        try:
            embedding = self.client.feature_extraction(text)
            if hasattr(embedding, "tolist"):
                return embedding.tolist()
            return embedding
        except Exception as e:
            raise Exception(f"HuggingFace Hub Error: {e}")


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
    err_msg = str(exc)
    trace = traceback.format_exc()
    print(f"ERROR: {err_msg}\n{trace}") # Crucial for server logs
    return JSONResponse(
        status_code=500,
        content={
            "detail": err_msg,
            "type": str(type(exc).__name__),
            "traceback": trace
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

    # SECURE LIMIT: Take only top 200 links to prevent "Device or resource busy"
    texts = texts[:200]
    hrefs = hrefs[:200]

    if not texts:
        raise HTTPException(status_code=400, detail="No links found on the target site.")

    # 1. Clear old data for THIS USER from all Supabase projects
    clients = get_supabase_clients()
    for name, sb in clients.items():
        try:
            # Only delete links belonging to this user
            sb.table("site_links").delete().eq("user_id", payload.user_id).execute()
        except Exception as e:
            print(f"Warning: Could not clear old links for {name}. Error: {e}")
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
            "embedding": vectors[i],
            "user_id": payload.user_id
        })
        
    # 4. Insert into all available Supabase projects in batches
    batch_size = 100
    success_count = 0
    errors = []
    
    for name, sb in clients.items():
        try:
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                time.sleep(1) # 1 second delay between batches for stability
                sb.table("site_links").insert(batch).execute()
            success_count += 1
            print(f"Success: Indexed to {name}")
        except Exception as e:
            err_msg = f"{name}: {str(e)}"
            print(f"Error: Failed to index to {err_msg}")
            errors.append(err_msg)
            
    if success_count == 0:
         detail_msg = "Database error: Failed to index website to any available database. Errors: " + " | ".join(errors)
         raise HTTPException(status_code=500, detail=detail_msg)

    _indexed_url = base_url

    return {"success": True, "indexed_url": _indexed_url, "links_count": len(records)}


@app.post("/api/search", response_model=SearchResponse)
def search(payload: SearchRequest):
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query cannot be empty.")

    embedder = get_embeddings()
    query_vector = embedder.embed_query(query)

    clients = get_supabase_clients()
    results = None
    last_error = None
    
    # Try Primary, Failover to others
    order = ["primary", "secondary"] if "secondary" in clients else ["primary"]
    
    for name in order:
        if name not in clients: continue
        try:
            sb = clients[name]
            response = sb.rpc(
                "match_links", 
                {
                    "query_embedding": query_vector, 
                    "match_threshold": 0.3, 
                    "match_count": 1,
                    "p_user_id": payload.user_id
                }
            ).execute()
            results = response.data
            if results is not None:
                print(f"Search Success via {name}")
                break
        except Exception as exc:
            print(f"Search Failed via {name}: {exc}")
            last_error = exc
            continue

    if results is None:
        raise HTTPException(status_code=500, detail=f"Database search failed across all servers: {last_error}")

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
        html_content = content.decode("utf-8", errors="replace")
        
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
    return Response(content=content, media_type=content_type, headers=forward_headers)
