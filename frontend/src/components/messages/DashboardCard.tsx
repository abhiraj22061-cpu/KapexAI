import { useNavigate } from 'react-router-dom'
import type { DashboardData } from '../../lib/types'
import type { MessageComponentProps } from './types'

/**
 * Persistent reference to a generated dashboard (message type `dashboard`).
 * Live stream frames carry the full `dashboard_data` payload, while history
 * entries only carry `dashboard_id` + `dashboard_name` — both open the
 * dashboard page, which fetches the document itself.
 */
export function DashboardCard({ message, sessionId }: MessageComponentProps) {
  const navigate = useNavigate()
  const dashboardId = message.dashboard_id as string | undefined
  const name = (message.dashboard_name as string | undefined) || 'Dashboard'
  const data = message.dashboard_data as DashboardData | undefined
  const summary = data?.summary || data?.intro

  function open() {
    if (dashboardId && sessionId) {
      navigate(`/chat/${sessionId}/dashboard/${dashboardId}`)
    }
  }

  return (
    <div className="tool-card dashboard-ref-card">
      <div className="tool-card-title">Dashboard ready</div>
      <div className="dashboard-ref-name">{name}</div>
      {summary ? <p className="dashboard-ref-summary">{summary}</p> : null}
      <button
        type="button"
        className="dashboard-open-btn"
        disabled={!dashboardId || !sessionId}
        onClick={open}
      >
        Open dashboard →
      </button>
    </div>
  )
}
