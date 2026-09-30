import { Markdown } from '../../lib/markdown'
import type { DashboardData } from '../../lib/types'

/**
 * Renders a dashboard document (built by the worker's DashboardBuilder) as a
 * report-style page: title block, sections (bullets or markdown text),
 * grounded suggestions and a source list. Pure CSS — no chart library.
 */
export function DashboardTemplate({ data }: { data: DashboardData }) {
  const sections = data.sections ?? []
  const suggestions = data.suggestions ?? []
  const sources = data.sources ?? []

  return (
    <article className="dashboard-doc">
      <header className="dashboard-doc-header">
        <span className="dashboard-kind-badge">{data.kind_label}</span>
        <h1 className="dashboard-doc-title">{data.title}</h1>
        {data.subtitle ? <p className="dashboard-doc-subtitle">{data.subtitle}</p> : null}
      </header>

      {data.intro ? <p className="dashboard-doc-intro">{data.intro}</p> : null}
      {data.summary ? <p className="dashboard-doc-summary">{data.summary}</p> : null}

      {sections.map((section, i) => (
        <section className="dashboard-section" key={`${section.heading}-${i}`}>
          <h2>{section.heading}</h2>
          {section.kind === 'bullets' ? (
            <ul>
              {(section.items ?? []).map((item, j) => (
                <li key={j}>{item}</li>
              ))}
            </ul>
          ) : (
            <Markdown>{section.text ?? ''}</Markdown>
          )}
        </section>
      ))}

      {suggestions.length > 0 && (
        <section className="dashboard-section dashboard-suggestions">
          <h2>Suggestions</h2>
          <ul>
            {suggestions.map((suggestion, i) => (
              <li key={i} className={`suggestion suggestion-${suggestion.origin}`}>
                <span className="suggestion-text">{suggestion.text}</span>
                {suggestion.source ? (
                  <a
                    className="suggestion-source"
                    href={suggestion.source.url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {suggestion.source.label}
                  </a>
                ) : (
                  <span className="suggestion-origin">AI insight</span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {sources.length > 0 && (
        <footer className="dashboard-sources">
          <h3>Sources</h3>
          <ul>
            {sources.map((source) => (
              <li key={source.url}>
                <a href={source.url} target="_blank" rel="noreferrer">
                  {source.label}
                </a>
              </li>
            ))}
          </ul>
        </footer>
      )}
    </article>
  )
}
