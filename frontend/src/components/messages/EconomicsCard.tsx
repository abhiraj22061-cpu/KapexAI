import type { ChatMessage } from '../../lib/types'
import { Markdown } from '../../lib/markdown'
import type { MessageComponentProps } from './types'

type Table = { headers: string[]; rows: string[][] }

/**
 * Renders live economic data (assistant message of type `economics`): the LLM
 * summary plus a compact table of the raw numbers returned by the API
 * (World Bank time series, BLS series, or Frankfurter exchange rates).
 */
export function EconomicsCard({ message }: MessageComponentProps) {
  const data = (message.data ?? {}) as Record<string, unknown>
  const source = message.source as string | undefined
  const table = tableFor(data)

  return (
    <div className="tool-card economics-card">
      <div className="tool-card-title">Economic data</div>
      <Markdown>{message.content ?? ''}</Markdown>

      {table ? (
        <table className="economics-table">
          <thead>
            <tr>
              {table.headers.map((h) => (
                <th key={h}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row, index) => (
              <tr key={index}>
                {row.map((cell, i) => (
                  <td key={i}>{cell}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <ProfileData data={data} />
      )}

      {source ? (
        <span className="economics-source">
          Source:{' '}
          <a href={source} target="_blank" rel="noreferrer">
            {source}
          </a>
        </span>
      ) : null}
    </div>
  )
}

function tableFor(data: Record<string, unknown>): Table | null {
  const observations = data.observations
  if (Array.isArray(observations) && observations.length) {
    return {
      headers: ['Year', 'Value', 'Country'],
      rows: observations.map((o) => {
        const obs = o as Record<string, unknown>
        return [
          String(obs.year ?? ''),
          obs.value == null ? '—' : String(obs.value),
          String(obs.country ?? ''),
        ]
      }),
    }
  }

  const series = data.series
  if (Array.isArray(series) && series.length) {
    const rows: string[][] = []
    for (const s of series) {
      const entry = s as { series_id?: unknown; observations?: unknown }
      const obs = Array.isArray(entry.observations) ? entry.observations : []
      for (const o of obs) {
        const point = o as Record<string, unknown>
        rows.push([
          String(point.year ?? ''),
          String(point.period_name ?? point.period ?? ''),
          point.value == null ? '—' : String(point.value),
        ])
      }
    }
    return rows.length ? { headers: ['Year', 'Period', 'Value'], rows } : null
  }

  const rates = data.rates
  if (Array.isArray(rates) && rates.length) {
    return {
      headers: ['Date', 'Base', 'Quote', 'Rate'],
      rows: rates.map((r) => {
        const rate = r as Record<string, unknown>
        return [
          String(rate.date ?? ''),
          String(rate.base ?? ''),
          String(rate.quote ?? ''),
          String(rate.rate ?? ''),
        ]
      }),
    }
  }

  return null
}

/** Country-profile payloads (metadata, no rows) render as a definition list. */
function ProfileData({ data }: { data: Record<string, unknown> }) {
  const fields: Array<[string, unknown]> = (
    [
      ['Country', data.country],
      ['Region', data.region],
      ['Income level', data.income_level],
      ['Lending type', data.lending_type],
      ['Capital', data.capital],
    ] as Array<[string, unknown]>
  ).filter(
    ([, value]) => value !== null && value !== undefined && value !== ''
  )

  if (!fields.length) return null
  return (
    <dl className="context-list">
      {fields.map(([label, value]) => (
        <div className="context-row" key={label}>
          <dt>{label}</dt>
          <dd>{String(value)}</dd>
        </div>
      ))}
    </dl>
  )
}
