import { useState, type FormEvent } from 'react'
import { joinWaitlist } from '../../lib/api'

export function WaitlistSection() {
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError('')
    setMessage('')
    setSubmitting(true)
    try {
      const result = await joinWaitlist(email, name || undefined)
      setMessage(result.message)
      setEmail('')
      setName('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not join the waitlist.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="waitlist" id="waitlist">
      <h2 className="waitlist-title">Join the waitlist</h2>
      <p className="waitlist-subtitle">
        Early access is rolling out soon. Drop your email and we&apos;ll let you know.
      </p>
      <form className="waitlist-form" onSubmit={handleSubmit}>
        <input
          type="text"
          className="waitlist-input"
          placeholder="Your name (optional)"
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <input
          type="email"
          className="waitlist-input"
          placeholder="you@example.com"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          required
        />
        <button type="submit" className="waitlist-btn" disabled={submitting}>
          {submitting ? 'Joining…' : 'Notify me'}
        </button>
      </form>
      {message && <p className="waitlist-success">{message}</p>}
      {error && (
        <p className="waitlist-error" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
