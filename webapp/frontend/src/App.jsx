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
  const [token, setToken] = useState(() => sessionStorage.getItem('staffToken') || '')
  const [needAuth, setNeedAuth] = useState(null) // null=unknown, true/false
  const [pw, setPw] = useState('')
  const [loginErr, setLoginErr] = useState('')
  const sessionId = useRef(`web-${Math.random().toString(36).slice(2)}`)
  const bottom = useRef(null)

  const authed = needAuth === false || (needAuth === true && token)

  function authHeaders() {
    return token ? { Authorization: `Bearer ${token}` } : {}
  }

  useEffect(() => {
    fetch(`${API}/api/auth_required`)
      .then(r => r.json())
      .then(d => setNeedAuth(d.required))
      .catch(() => setNeedAuth(false))
  }, [])

  useEffect(() => {
    if (!authed) return
    fetch(`${API}/api/patients`, { headers: authHeaders() })
      .then(r => (r.ok ? r.json() : []))
      .then(setPatients)
      .catch(() => {})
    fetch(`${API}/api/rules`).then(r => r.json()).then(setRules).catch(() => {})
  }, [authed, token])

  async function login(e) {
    e.preventDefault()
    setLoginErr('')
    try {
      const r = await fetch(`${API}/api/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: pw }),
      })
      if (!r.ok) {
        setLoginErr('Incorrect password')
        return
      }
      const { token: t } = await r.json()
      sessionStorage.setItem('staffToken', t)
      setToken(t)
      setPw('')
    } catch {
      setLoginErr('Login failed — is the backend running?')
    }
  }

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
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ session_id: sessionId.current, message }),
      })
      if (r.status === 401) {
        sessionStorage.removeItem('staffToken')
        setToken('')
        return
      }
      const data = await r.json()
      setMessages(m => [
        ...m,
        {
          role: 'assistant',
          content: data.reply || 'Something went wrong.',
          slots: data.slots || [],
          booked: data.booked || null,
        },
      ])
    } catch {
      setMessages(m => [...m, { role: 'assistant', content: 'Backend unreachable — is uvicorn running on :8000?' }])
    } finally {
      setBusy(false)
    }
  }

  function pickSlot(mi, slot) {
    setMessages(m => m.map((msg, i) => (i === mi ? { ...msg, chosen: slot.start } : msg)))
    send(
      `Book the ${slot.label} slot (category ${slot.category}, start_time ${slot.start}, provider_id ${slot.provider_id}${
        slot.operatory_id ? `, operatory_id ${slot.operatory_id}` : ''
      }). Yes, that time is confirmed.`,
    )
  }

  if (needAuth === null) return <div className="loading">Loading…</div>

  if (needAuth && !token) {
    return (
      <div className="login-screen">
        <form className="login-card" onSubmit={login}>
          <h1>🦷 Green River Dental</h1>
          <p>Staff scheduling assistant</p>
          <input
            type="password"
            placeholder="Staff password"
            value={pw}
            onChange={e => setPw(e.target.value)}
            autoFocus
          />
          <button type="submit">Sign in</button>
          {loginErr && <div className="login-err">{loginErr}</div>}
          <div className="login-note">
            This tool shows protected patient information and is for authorized staff only.
          </div>
        </form>
      </div>
    )
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
            <div key={i} className={`bubble-group ${m.role}`}>
              <div className={`msg ${m.role}`}>{m.content}</div>
              {m.slots?.length > 0 && (
                <div className="slots">
                  {m.slots.map(s => (
                    <button
                      key={s.start + s.provider_id}
                      className={`slot-pill ${m.chosen === s.start ? 'chosen' : ''}`}
                      disabled={busy || m.chosen}
                      onClick={() => pickSlot(i, s)}
                    >
                      {s.label}
                    </button>
                  ))}
                </div>
              )}
              {m.booked && (
                <div className="booked-card">
                  ✅ Booked — appt #{m.booked.appointment_id} · {m.booked.minutes} min
                  <div className="booked-note">{m.booked.note_on_appointment}</div>
                </div>
              )}
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
