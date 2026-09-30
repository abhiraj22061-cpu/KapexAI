import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { DashboardTemplate } from '../components/dashboard/DashboardTemplate'
import { getDashboard, getDashboards } from '../lib/api'
import { useAuth } from '../lib/auth'
import type { DashboardDetail, DashboardInfo } from '../lib/types'

function formatDate(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

/**
 * Read-only dashboard viewer: `/chat/:sessionId/dashboard/:dashboardId`.
 * The header links back to the conversation (`?session=` restores it) and the
 * tab bar switches between every dashboard generated for that session.
 */
export function DashboardPage() {
  const { token } = useAuth()
  const { sessionId = '', dashboardId = '' } = useParams()
  const navigate = useNavigate()
  const [tabs, setTabs] = useState<DashboardInfo[]>([])
  const [dashboard, setDashboard] = useState<DashboardDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const backToChat = useCallback(() => {
    navigate(sessionId ? `/chat?session=${encodeURIComponent(sessionId)}` : '/chat')
  }, [navigate, sessionId])

  useEffect(() => {
    if (!token || !dashboardId) return
    let cancelled = false
    setLoading(true)
    setError(null)
    Promise.all([getDashboards(token, sessionId), getDashboard(token, dashboardId)])
      .then(([list, detail]) => {
        if (cancelled) return
        setTabs(list.data)
        setDashboard(detail.data)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Could not load the dashboard')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [token, sessionId, dashboardId])

  return (
    <div className="dashboard-page">
      <header className="dashboard-page-header">
        <button type="button" className="dashboard-back-btn" onClick={backToChat}>
          ← Chat
        </button>
        <h1 className="dashboard-page-title">{dashboard?.name || 'Dashboard'}</h1>
        {dashboard ? (
          <span className="dashboard-page-date">{formatDate(dashboard.created_at)}</span>
        ) : null}
      </header>

      {tabs.length > 1 ? (
        <nav className="dashboard-tabs" aria-label="Dashboards">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              className={`dashboard-tab${tab.id === dashboardId ? ' active' : ''}`}
              onClick={() =>
                navigate(`/chat/${encodeURIComponent(sessionId)}/dashboard/${tab.id}`)
              }
            >
              {tab.name}
            </button>
          ))}
        </nav>
      ) : null}

      <div className="dashboard-scroll">
        {loading ? (
          <div className="dashboard-state">
            <div className="spinner" aria-label="Loading" />
          </div>
        ) : error ? (
          <div className="dashboard-state dashboard-error">
            <p>{error}</p>
            <button type="button" className="dashboard-back-btn" onClick={backToChat}>
              ← Back to chat
            </button>
          </div>
        ) : dashboard ? (
          <DashboardTemplate data={dashboard.data} />
        ) : (
          <div className="dashboard-state dashboard-error">
            <p>Dashboard not found.</p>
            <button type="button" className="dashboard-back-btn" onClick={backToChat}>
              ← Back to chat
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
