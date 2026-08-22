import { Markdown } from '../../lib/markdown'
import type { MessageComponentProps } from './types'

type Scenario = {
  name?: unknown
  description?: unknown
  probability?: unknown
  signals?: unknown
  impacts?: unknown
  trigger_actions?: unknown
}

/**
 * Renders a foresight analysis (assistant message of type `foresight`): the
 * LLM summary plus either a structured scenario matrix (with probabilities,
 * early-warning signals and trigger actions) or the raw JPL Horizons ephemeris
 * with its scientific disclaimer.
 */
export function ForesightCard({ message }: MessageComponentProps) {
  const data = (message.data ?? {}) as Record<string, unknown>
  const source = message.source as string | undefined
  const scenarios = Array.isArray(data.scenarios) ? data.scenarios : []
  const ephemeris = typeof data.ephemeris === 'string' ? data.ephemeris : ''

  return (
    <div className="tool-card foresight-card">
      <div className="tool-card-title">Foresight analysis</div>
      <Markdown>{message.content ?? ''}</Markdown>

      {scenarios.length > 0 ? (
        <ScenarioMatrix
          scenarios={scenarios as Scenario[]}
          total={data.probability_total}
          sumsToOne={data.probabilities_sum_to_one}
          guidance={data.guidance}
        />
      ) : null}

      {ephemeris ? <EphemerisBlock data={data} ephemeris={ephemeris} /> : null}

      {source ? (
        <span className="foresight-source">
          Source:{' '}
          <a href={source} target="_blank" rel="noreferrer">
            {source}
          </a>
        </span>
      ) : null}
    </div>
  )
}

function ScenarioMatrix({
  scenarios,
  total,
  sumsToOne,
  guidance,
}: {
  scenarios: Scenario[]
  total?: unknown
  sumsToOne?: unknown
  guidance?: unknown
}) {
  const totalLabel = typeof total === 'number' ? pct(total) : null
  return (
    <div className="scenario-matrix">
      {scenarios.map((scenario, index) => (
        <div className="scenario-block" key={index}>
          <div className="scenario-head">
            <span className="scenario-name">
              {scenario.name ? String(scenario.name) : `Scenario ${index + 1}`}
            </span>
            <span className="scenario-prob">{probabilityLabel(scenario.probability)}</span>
          </div>
          {scenario.description ? (
            <p className="scenario-desc">{String(scenario.description)}</p>
          ) : null}
          <ScenarioList label="Early signals" items={scenario.signals} />
          <ScenarioList label="Impacts" items={scenario.impacts} />
          <ScenarioList label="Trigger actions" items={scenario.trigger_actions} />
        </div>
      ))}
      <p className="scenario-sum">
        {totalLabel ? `Total probability: ${totalLabel}` : null}
        {sumsToOne === true ? ' · scenarios cover the full space' : ' · partial coverage'}
      </p>
      {guidance ? <p className="scenario-guidance">{String(guidance)}</p> : null}
    </div>
  )
}

function ScenarioList({ label, items }: { label: string; items?: unknown }) {
  const list = Array.isArray(items) ? items.filter((i) => i != null && i !== '') : []
  if (!list.length) return null
  return (
    <div className="scenario-list">
      <span className="scenario-list-label">{label}:</span>
      <ul>
        {list.map((item, index) => (
          <li key={index}>{String(item)}</li>
        ))}
      </ul>
    </div>
  )
}

function EphemerisBlock({ data, ephemeris }: { data: Record<string, unknown>; ephemeris: string }) {
  const disclaimer = typeof data.disclaimer === 'string' ? data.disclaimer : ''
  return (
    <div className="ephemeris-block">
      <pre className="ephemeris-pre">{ephemeris}</pre>
      {disclaimer ? <p className="ephemeris-disclaimer">{disclaimer}</p> : null}
    </div>
  )
}

function probabilityLabel(probability?: unknown): string {
  if (typeof probability === 'number') return pct(probability)
  if (typeof probability === 'string' && probability.trim() !== '') return probability
  return '—'
}

function pct(probability: number): string {
  const rounded = Math.round(probability * 1000) / 10
  return `${rounded}%`
}