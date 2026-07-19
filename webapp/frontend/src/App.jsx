import { useEffect, useRef, useState } from 'react'

const API = import.meta.env.VITE_API_URL || ''

export default function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem('patientToken') || '')
  const [patientName, setPatientName] = useState(() => sessionStorage.getItem('patientName') || '')
  const [rules, setRules] = useState(null)

  // verify form
  const [name, setName] = useState('')
  const [dob, setDob] = useState('')
  const [verifyErr, setVerifyErr] = useState('')
  const [verifying, setVerifying] = useState(false)

  // chat
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const bottom = useRef(null)

  useEffect(() => {
    fetch(`${API}/api/rules`).then(r => r.json()).then(setRules).catch(() => {})
  }, [])

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    if (token && messages.length === 0) {
      setMessages([
        {
          role: 'assistant',
          content: `Hi ${patientName.split(' ')[0]}! I'm the Green River Dental assistant. I can tell you what you'll owe for your treatment, show your appointments, or help you book, reschedule, or cancel. What can I do for you?`,
        },
      ])
    }
  }, [token]) // eslint-disable-line

  async function verify(e) {
    e.preventDefault()
    setVerifyErr('')
    setVerifying(true)
    try {
      const r = await fetch(`${API}/api/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim(), date_of_birth: dob }),
      })
      if (!r.ok) {
        const d = await r.json().catch(() => ({}))
        setVerifyErr(d.detail || 'We could not verify you.')
        return
      }
      const d = await r.json()
      sessionStorage.setItem('patientToken', d.token)
      sessionStorage.setItem('patientName', d.patient_name)
      setPatientName(d.patient_name)
      setToken(d.token)
    } catch {
      setVerifyErr('Something went wrong — please try again.')
    } finally {
      setVerifying(false)
    }
  }

  function signOut() {
    sessionStorage.clear()
    setToken('')
    setMessages([])
    setName('')
    setDob('')
  }

  async function send(text) {
    const message = (text ?? input).trim()
    if (!message || busy) return
    setInput('')
    setMessages(m => [...m, { role: 'user', content: message }])
    setBusy(true)
    try {
      const r = await fetch(`${API}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ message }),
      })
      if (r.status === 401) {
        signOut()
        return
      }
      const data = await r.json()
      setMessages(m => [
        ...m,
        { role: 'assistant', content: data.reply || '…', slots: data.slots || [], booked: data.booked || null },
      ])
    } catch {
      setMessages(m => [...m, { role: 'assistant', content: 'Connection problem — please try again.' }])
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

  // ---------------------------------------------------------------- identity gate
  if (!token) {
    return (
      <div className="verify-screen">
        <form className="verify-card" onSubmit={verify}>
          <h1>🦷 Green River Dental</h1>
          <p className="sub">Let's confirm it's really you.</p>
          <label>Full name</label>
          <input value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Sharon Mascari" autoFocus />
          <label>Date of birth</label>
          <input type="date" value={dob} onChange={e => setDob(e.target.value)} />
          <button type="submit" disabled={verifying || !name.trim() || !dob}>
            {verifying ? 'Verifying…' : 'Continue'}
          </button>
          {verifyErr && <div className="verify-err">{verifyErr}</div>}
          <div className="verify-note">
            We verify your identity to protect your health information. This assistant only
            shows your own account.
          </div>
        </form>
      </div>
    )
  }

  // ---------------------------------------------------------------- chat
  return (
    <div className="chat-app">
      <header className="topbar">
        <span className="brand">🦷 Green River Dental</span>
        <span className="who">
          {patientName}
          <button className="signout" onClick={signOut}>
            Sign out
          </button>
        </span>
      </header>

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
                ✅ You're booked — {m.booked.minutes} min · confirmation #{m.booked.appointment_id}
                <div className="booked-note">{m.booked.note_on_appointment}</div>
              </div>
            )}
          </div>
        ))}
        {busy && <div className="msg assistant typing">…</div>}
        <div ref={bottom} />
      </div>

      <div className="quickies">
        {['What will I owe?', 'When is my appointment?', 'Reschedule my visit'].map(q => (
          <button key={q} onClick={() => send(q)} disabled={busy}>
            {q}
          </button>
        ))}
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
          placeholder="Type your message…"
          disabled={busy}
        />
        <button disabled={busy || !input.trim()}>Send</button>
      </form>
    </div>
  )
}
