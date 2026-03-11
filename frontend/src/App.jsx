import { useEffect, useRef, useState } from 'react'
import { createClient } from '@supabase/supabase-js'

// Initialize Supabase client safely with hardcoded fallbacks for immediate deployment
const supabaseUrl = import.meta.env.VITE_SUPABASE_URL || "https://ahgeogqptinlymmcuvyg.supabase.co"
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY || "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFoZ2VvZ3FwdGlubHltbWN1dnlnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzMyMjUzODIsImV4cCI6MjA4ODgwMTM4Mn0.XddD4uxcg9x8L7KBh4kMgHnM5mq2bHw3xk8mZ4l5nvc"
const supabase = createClient(supabaseUrl, supabaseAnonKey)

const proxyUrl = (url) => `/api/proxy?url=${encodeURIComponent(url)}`

export default function App() {
  // ── Auth State ──
  const [session, setSession] = useState(null)
  const [authView, setAuthView] = useState('login') // 'login' | 'signup'
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [fullName, setFullName] = useState('')
  const [phone, setPhone] = useState('')
  const [authError, setAuthError] = useState('')
  const [loading, setLoading] = useState(false)

  // ── Phase: 'setup' | 'indexing' | 'ready' ──
  const [phase, setPhase] = useState('setup')
  const [urlInput, setUrlInput] = useState('')
  const [indexError, setIndexError] = useState('')
  const [siteUrl, setSiteUrl] = useState('')
  const [linksCount, setLinksCount] = useState(0)
  const [iframeSrc, setIframeSrc] = useState('')

  // ── Chat state ──
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [searching, setSearching] = useState(false)
  const inputRef = useRef(null)
  const urlInputRef = useRef(null)

  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      setSession(session)
    })

    const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, session) => {
      setSession(session)
    })

    return () => subscription.unsubscribe()
  }, [])

  // Catch URL errors (like expired links) on mount
  useEffect(() => {
    const hash = window.location.hash
    if (hash && hash.includes('error=')) {
      const params = new URLSearchParams(hash.replace('#', '?'))
      const errorDesc = params.get('error_description')
      if (errorDesc) {
        setAuthError(`Auth Error: ${errorDesc.replace(/\+/g, ' ')} (The link may have expired)`)
      }
    }
  }, [])

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 80)
  }, [open])

  // Listen for link-click messages from the proxied iframe
  useEffect(() => {
    const handler = (e) => {
      if (e.data?.type === 'PROXY_NAVIGATE' && e.data.url) {
        setIframeSrc(proxyUrl(e.data.url))
        setResult(null)
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [])

  // ── Index a website ──
  const handleIndex = async (e) => {
    e.preventDefault()
    setIndexError('')
    if (!urlInput.trim()) {
      setIndexError('Please enter a URL.')
      return
    }
    setPhase('indexing')
    try {
      const res = await fetch('/api/index', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          url: urlInput.trim(),
          user_id: session.user.id 
        })
      })
      
      let data = {}
      const contentType = res.headers.get("content-type")
      if (contentType && contentType.includes("application/json")) {
        data = await res.json()
      } else {
        const text = await res.text()
        throw new Error(text || 'Server returned an error without JSON details.')
      }

      if (!res.ok) {
        setPhase('setup')
        const details = data.detail || `Server Error (${res.status})`
        const traceback = data.traceback ? `\n\nTraceback:\n${data.traceback}` : ''
        setIndexError(`${details}${traceback}`)
        return
      }
      setSiteUrl(data.indexed_url)
      setLinksCount(data.links_count)
      setIframeSrc(proxyUrl(data.indexed_url))
      setPhase('ready')
    } catch (err) {
      setPhase('setup')
      setIndexError(err.message || 'Could not connect to backend. Is the server running?')
    }
  }

  // ── Search ──
  const onSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setResult(null)
    if (!query.trim()) {
      setError('Please enter where you want to go.')
      return
    }
    setSearching(true)
    try {
      const res = await fetch('/api/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          query,
          user_id: session.user.id
        })
      })
      const data = await res.json()
      if (!res.ok) {
        setError(data.detail || 'No match found.')
        return
      }
      setResult(data)
      setIframeSrc(proxyUrl(data.url))
    } catch {
      setError('Search request failed. Please try again.')
    } finally {
      setSearching(false)
    }
  }

  const handleAuth = async (e) => {
    e.preventDefault()
    setAuthError('')
    setLoading(true)
    try {
      if (authView === 'signup') {
        const { data, error } = await supabase.auth.signUp({
          email,
          password,
        })
        if (error) throw error
        
        // After signup, save name and phone to profiles
        if (data.user) {
          const { error: profileError } = await supabase
            .from('profiles')
            .upsert({ 
              id: data.user.id,
              full_name: fullName,
              phone_number: phone,
              updated_at: new Date().toISOString()
            })
          if (profileError) console.error("Profile error:", profileError)
        }
        alert("Account created! You can now log in.")
        setAuthView('login')
      } else {
        const { error } = await supabase.auth.signInWithPassword({
          email,
          password,
        })
        if (error) throw error
      }
    } catch (err) {
      if (err.message === 'Failed to fetch' || err.message.includes('NetworkError')) {
        setAuthError('Connection Failed: Your Supabase project might be PAUSED. Please go to your Supabase Dashboard and click "Resume".')
      } else {
        setAuthError(err.message)
      }
    } finally {
      setLoading(false)
    }
  }

  const resetToSetup = () => {
    setPhase('setup')
    setSiteUrl('')
    setLinksCount(0)
    setIframeSrc('')
    setUrlInput('')
    setResult(null)
    setError('')
    setOpen(false)
  }

  const handleSignOut = async () => {
    await supabase.auth.signOut()
    resetToSetup()
  }

  // ══════════════════════════════════════════════
  // AUTH PAGE
  // ══════════════════════════════════════════════
  if (!session) {
    return (
      <div className="setup-page">
        <div className="setup-card">
          <div className="setup-icon">👤</div>
          <h1 className="setup-title">{authView === 'login' ? 'Login' : 'Create Account'}</h1>
          <form onSubmit={handleAuth} className="setup-form">
            <input
              type="email"
              placeholder="Email ID"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="setup-input"
              required
            />
            {authView === 'signup' && (
              <>
                <input
                  type="text"
                  placeholder="Full Name"
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  className="setup-input"
                  required
                />
                <input
                  type="tel"
                  placeholder="Phone Number"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  className="setup-input"
                  required
                />
              </>
            )}
            <input
              type="password"
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="setup-input"
              required
            />
            <button type="submit" disabled={loading} className="setup-btn">
              {loading ? 'Processing...' : (authView === 'login' ? 'Sign In' : 'Register')}
            </button>
          </form>
          {authError && <p className="setup-error">{authError}</p>}
          <button 
            className="btn-change" 
            style={{ marginTop: '1rem', background: 'transparent', border: 'none', color: '#8b8efc' }}
            onClick={() => setAuthView(authView === 'login' ? 'signup' : 'login')}
          >
            {authView === 'login' ? "Don't have an account? Sign up" : "Already have an account? Login"}
          </button>
          {authError && (
            <button 
              className="setup-btn" 
              style={{ marginTop: '1rem', background: '#3a3d4e' }}
              onClick={() => { window.location.hash = ''; window.location.reload() }}
            >
              🔄 Try Again / Back to Login
            </button>
          )}
        </div>
      </div>
    )
  }

  // ══════════════════════════════════════════════
  // SETUP / INDEXING PAGE
  // ══════════════════════════════════════════════
  if (phase === 'setup' || phase === 'indexing') {
    return (
      <div className="setup-page">
        <div className="setup-card">
          <div className="setup-icon">🤖</div>
          <h1 className="setup-title">AI Website Navigator</h1>
          <p className="setup-sub">
            Paste your college or organization's URL — the agent will crawl it
            and answer navigation questions.
          </p>

          <form onSubmit={handleIndex} className="setup-form">
            <input
              ref={urlInputRef}
              type="text"
              value={urlInput}
              onChange={(e) => setUrlInput(e.target.value)}
              placeholder="https://yourcollege.edu.in"
              disabled={phase === 'indexing'}
              className="setup-input"
              autoFocus
            />
            <button type="submit" disabled={phase === 'indexing'} className="setup-btn">
              {phase === 'indexing'
                ? <><span className="spinner" /> Indexing…</>
                : 'Index & Launch →'}
            </button>
          </form>

          {indexError && <p className="setup-error">{indexError}</p>}

          {phase === 'indexing' && (
            <p className="indexing-msg">🔍 Crawling website and building knowledge base…</p>
          )}
        </div>
      </div>
    )
  }

  // ══════════════════════════════════════════════
  // READY PAGE — full-screen iframe + chat widget
  // ══════════════════════════════════════════════
  return (
    <>
      {/* Full-screen site iframe via proxy */}
      <iframe
        src={iframeSrc}
        title="Indexed Website"
        className="site-frame"
        sandbox="allow-forms allow-modals allow-popups allow-presentation allow-same-origin allow-scripts"
      />

      {/* Floating widget */}
      <div className="widget-container">
        {open && (
          <div className="chat-popup">
            <div className="chat-header">
              <span>🤖 AI Navigator</span>
              <button className="close-btn" onClick={() => setOpen(false)}>✕</button>
            </div>

            <div className="chat-body">
              <p className="site-tag">📌 {siteUrl}</p>
              <p className="links-tag">{linksCount} links indexed</p>

              <form onSubmit={onSubmit} className="search-form">
                <label htmlFor="query">Where do you want to go?</label>
                <div className="row">
                  <input
                    ref={inputRef}
                    id="query"
                    type="text"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="e.g. placement department"
                    disabled={searching}
                  />
                  <button type="submit" disabled={searching}>
                    {searching ? '…' : '➤'}
                  </button>
                </div>
              </form>

              {error && <p className="text-error">{error}</p>}

              {result && (
                <div className="result">
                  <p>Matched: <strong>{result.label}</strong></p>
                  <a href={result.url} target="_blank" rel="noreferrer" className="result-url">
                    {result.url} ↗
                  </a>
                  <button onClick={() => { setIframeSrc(proxyUrl(siteUrl)); setResult(null) }} className="btn-secondary">↩ Back to Home</button>
                </div>
              )}

              <button onClick={handleSignOut} className="btn-change">
                🚪 Sign Out
              </button>
              <button onClick={resetToSetup} className="btn-change">
                ↩ Change Website
              </button>
            </div>
          </div>
        )}

        <button className="chat-bubble" onClick={() => setOpen(p => !p)} title="AI Navigator">
          {open ? '✕' : '🤖'}
        </button>
      </div>
    </>
  )
}
