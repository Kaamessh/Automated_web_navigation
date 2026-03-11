import { useEffect, useRef, useState } from 'react'

const proxyUrl = (url) => `/api/proxy?url=${encodeURIComponent(url)}`

export default function App() {
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
        body: JSON.stringify({ url: urlInput.trim() })
      })
      const data = await res.json()
      if (!res.ok) {
        setPhase('setup')
        setIndexError(data.detail || 'Failed to index website.')
        return
      }
      setSiteUrl(data.indexed_url)
      setLinksCount(data.links_count)
      setIframeSrc(proxyUrl(data.indexed_url))
      setPhase('ready')
    } catch {
      setPhase('setup')
      setIndexError('Could not connect to backend. Is the server running?')
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
        body: JSON.stringify({ query })
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

  const resetToSetup = () => {
    setPhase('setup')
    setResult(null)
    setQuery('')
    setError('')
    setOpen(false)
    setUrlInput('')
    setIframeSrc('')
    setTimeout(() => urlInputRef.current?.focus(), 80)
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
