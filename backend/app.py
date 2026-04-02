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


@app.get("/api/suggestions")
async def get_suggestions(query: str = Query(..., min_length=1)):
    """
    Advanced Server-side suggestion engine (Google + DuckDuckGo + Clearbit).
    Handles typos, abbreviations, and fetches logos.
    """
    if not query or len(query.strip()) < 2:
        return []

    search_terms = {query.lower().strip()}
    all_results = []

    # 1. Fetch fuzzy suggestions from DuckDuckGo
    try:
        ddg_url = f"https://duckduckgo.com/ac/?q={urllib.parse.quote(query)}"
        ddg_resp = requests.get(ddg_url, timeout=3)
        if ddg_resp.ok:
            for item in ddg_resp.json():
                if "phrase" in item:
                    search_terms.add(item["phrase"].lower().strip())
    except Exception as e:
        print(f"DDG Suggestion Error: {e}")

    # 2. Fetch fuzzy suggestions from Google (Gold Standard for typos)
    try:
        google_url = f"https://suggestqueries.google.com/complete/search?client=chrome&q={urllib.parse.quote(query)}"
        google_resp = requests.get(google_url, timeout=3)
        if google_resp.ok:
            # Google returns ["query", ["sug1", "sug2"], ...]
            data = google_resp.json()
            if len(data) > 1 and isinstance(data[1], list):
                for sug in data[1][:3]:
                    search_terms.add(sug.lower().strip())
    except Exception as e:
        print(f"Google Suggestion Error: {e}")

    # 3. Use search terms to get high-quality domain info from Clearbit
    # We prioritize the original query first
    ordered_terms = sorted(list(search_terms), key=lambda x: 0 if x == query.lower() else 1)
    
    seen_domains = set()
    for term in ordered_terms[:5]: # Check top 5 fuzzy variations
        try:
            cb_url = f"https://autocomplete.clearbit.com/v1/companies/suggest?query={urllib.parse.quote(term)}"
            cb_resp = requests.get(cb_url, timeout=3)
            if cb_resp.ok:
                for item in cb_resp.json():
                    domain = item.get("domain")
                    if domain and domain not in seen_domains:
                        all_results.append(item)
                        seen_domains.add(domain)
        except:
            continue
            
    # 4. Final Fallback: If still nothing, try a very broad search on the first word
    if not all_results and " " in query:
        first_word = query.split(" ")[0]
        try:
            cb_url = f"https://autocomplete.clearbit.com/v1/companies/suggest?query={urllib.parse.quote(first_word)}"
            cb_resp = requests.get(cb_url, timeout=3)
            if cb_resp.ok:
                for item in cb_resp.json():
                    domain = item.get("domain")
                    if domain and domain not in seen_domains:
                        all_results.append(item)
                        seen_domains.add(domain)
        except:
            pass

    return all_results[:8]


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
        
    # 4. Insert into all available Supabase projects using Direct REST (Ultra-Light)
    # This bypasses the heavy supabase-py library to fix [Errno 16] "Busy" errors.
    batch_size = 100
    success_count = 0
    errors = []
    
    for name, sb in clients.items():
        try:
            url = SUPABASE_URL if name == "primary" else SUPABASE_URL_2
            key = SUPABASE_KEY if name == "primary" else SUPABASE_KEY_2
            
            # Direct REST endpoint for Supabase
            rest_url = f"{url}/rest/v1/site_links"
            rest_headers = {
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            }
            
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                time.sleep(0.5) # Small breath
                resp = requests.post(rest_url, headers=rest_headers, json=batch, timeout=30)
                resp.raise_for_status()
                
            success_count += 1
            print(f"Success: Indexed to {name} via Direct REST")
        except Exception as e:
            err_details = str(e)
            if hasattr(e, 'response') and e.response is not None:
                err_details += f" | Response: {e.response.text}"
            err_msg = f"{name}: {err_details}"
            print(f"Error: {err_msg}")
            errors.append(err_msg)
            
    if success_count == 0:
         detail_msg = "Database error: All database attempts failed. Details: " + " | ".join(errors)
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
        html_content = content.decode("utf-8", errors="replace")
        
        # 1. FIX MIXED CONTENT: Force http -> https for the target domain and its assets
        parsed_url = urllib.parse.urlparse(url)
        domain = parsed_url.netloc
        import re
        html_content = re.sub(r'http://' + re.escape(domain), r'https://' + domain, html_content, flags=re.I)
        # General http to https for common asset CDNs/libraries
        html_content = re.sub(r'http://(www\.)?google-analytics\.com', r'https://\1google-analytics.com', html_content, flags=re.I)
        html_content = re.sub(r'http://(www\.)?googletagmanager\.com', r'https://\1googletagmanager.com', html_content, flags=re.I)

        soup = BeautifulSoup(html_content, "html.parser")
        
        # 2. BASE TAG INJECTION: Fix relative paths for images/css/js
        if not soup.find("base"):
            base_tag = soup.new_tag("base", href=url)
            if soup.head:
                soup.head.insert(0, base_tag)
            elif soup.html:
                soup.html.insert(0, base_tag)

        # 3. SPOOF SCRIPT (Frame-Busting Shield)
        spoof_script = soup.new_tag("script")
        spoof_script.string = """
(function() {
  // Spoof window hierarchy to prevent frame-busting
  try {
    Object.defineProperty(window, 'top', { get: function() { return window; }, configurable: false });
    Object.defineProperty(window, 'parent', { get: function() { return window; }, configurable: false });
    window.self = window;
    window.frameElement = null;
  } catch (e) {}

  // Intercept all link clicks to prevent them from breaking out
  document.addEventListener('click', function(e) {
      var target = e.target.closest('a');
      if (target && target.target === '_top') {
          target.target = '_self';
      }
  }, true);
})();
"""
        if soup.head:
            soup.head.append(spoof_script)
        elif soup.html:
            soup.html.append(spoof_script)

        # 4. INTERCEPTOR SCRIPT (Navigation & Proxying)
        interceptor = soup.new_tag("script")
        interceptor.string = """
(function () {
  const BASE_URL = '""" + url + """';
  const PROXY_ROOT = window.location.origin + '/api/proxy?url=';

  function resolve(url) {
    if (!url) return url;
    if (url.startsWith('data:') || url.startsWith('blob:') || url.startsWith('javascript:')) return url;
    if (url.startsWith('//')) return (BASE_URL.startsWith('https:') ? 'https:' : 'http:') + url;
    try { return new URL(url, BASE_URL).href; } catch (_) { return url; }
  }

  function proxify(url) {
    const resolved = resolve(url);
    if (!resolved || resolved.startsWith('data:') || resolved.startsWith('blob:') || resolved.startsWith('javascript:')) return url;
    if (resolved.startsWith(window.location.origin + '/api/proxy')) return resolved;
    return PROXY_ROOT + encodeURIComponent(resolved);
  }

  // Intercept Link Clicks and communicate with parent
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

  // Patch dynamic requests
  const originalFetch = window.fetch;
  window.fetch = function(input, init) {
    if (typeof input === 'string') input = proxify(input);
    else if (input instanceof Request) {
      input = new Request(proxify(input.url), input);
    }
    return originalFetch(input, init);
  };

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

        final_body = str(soup).encode("utf-8", errors="replace")
        return Response(content=final_body, media_type="text/html; charset=utf-8")

    # Non-HTML (images, CSS, JS, fonts…) — pass through as-is
    forward_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in _BLOCKED_HEADERS
        and k.lower() not in ("transfer-encoding", "connection")
    }
    return Response(content=content, media_type=content_type, headers=forward_headers)
