import { useEffect, useRef, useState } from 'react'

const API = import.meta.env.VITE_API_URL || ''

export default function App() {
  const [messages, setMessages] = useState([
    {
      role: 'assistant',
      content:
        "Hi! I'm the Green River Dental scheduling assistant. Ask me about a patient's out-of-pocket estimate, open slots, or book an appointment — I follow the office rules automatically.",
    },
  ])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [patients, setPatients] = useState([])
  const [rules, setRules] = useState(null)
  const sessionId = useRef(`web-${Math.random().toString(36).slice(2)}`)
  const bottom = useRef(null)

  useEffect(() => {
    fetch(`${API}/api/patients`).then(r => r.json()).then(setPatients).catch(() => {})
    fetch(`${API}/api/rules`).then(r => r.json()).then(setRules).catch(() => {})
  }, [])

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function send(text) {
    const message = (text ?? input).trim()
    if (!message || busy) return
    setInput('')
    setMessages(m => [...m, { role: 'user', content: message }])
    setBusy(true)
    try {
      const r = await fetch(`${API}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId.current, message }),
      })
      const data = await r.json()
      setMessages(m => [...m, { role: 'assistant', content: data.reply || 'Something went wrong.' }])
    } catch {
      setMessages(m => [...m, { role: 'assistant', content: 'Backend unreachable — is uvicorn running on :8000?' }])
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <h1>🦷 {rules?.practice?.name || 'Scheduling Assistant'}</h1>
        {rules && (
          <div className="rules">
            <h2>Office rules</h2>
            <ul>
              <li>Implants: referred out</li>
              <li>
                {rules.afternoon_cutoff.applies_to.join(', ')} — start before{' '}
                {rules.afternoon_cutoff.time}
              </li>
              <li>
                Hours {rules.practice.open}–{rules.practice.close}, Mon–Fri
              </li>
            </ul>
          </div>
        )}
        <h2>Campaign patients</h2>
        <div className="patients">
          {patients.map(p => (
            <button
              key={p.name}
              className="patient"
              onClick={() => send(`How much will ${p.name} pay out of pocket?`)}
              title="Ask about this patient"
            >
              <span className="pname">{p.name}</span>
              <span className={`pill ${p.coverage.startsWith('ACTIVE') ? 'ok' : 'warn'}`}>
                {p.category}
              </span>
              <span className="oop">${p.oop}</span>
            </button>
          ))}
        </div>
      </aside>

      <main className="chat">
        <div className="messages">
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              {m.content}
            </div>
          ))}
          {busy && <div className="msg assistant typing">…</div>}
          <div ref={bottom} />
        </div>
        <form
          className="composer"
          onSubmit={e => {
            e.preventDefault()
            send()
          }}
        >
          <input
            value={input}
            onChange={e => setInput(e.target.value)}
            placeholder='e.g. "Book Sharon Mascari&apos;s crown Tuesday morning"'
            disabled={busy}
          />
          <button disabled={busy || !input.trim()}>Send</button>
        </form>
      </main>
    </div>
  )
}
