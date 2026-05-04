"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";


function formatPercent(value) {
  return `${(Number(value || 0) * 100).toFixed(2)}%`;
}


function getRollingWindow(scanHistory, minutes = 60) {
  const cutoff = Date.now() - minutes * 60 * 1000;
  return (Array.isArray(scanHistory) ? scanHistory : []).filter((item) => {
    const at = new Date(item?.timestamp || 0).getTime();
    return Number.isFinite(at) && at >= cutoff;
  });
}


function buildRejectMatrix(scanHistory, symbols) {
  const rows = new Map();
  const causes = new Set();

  symbols.forEach((symbol) => {
    rows.set(symbol, { symbol, total: 0, iaConsulted: 0, infraImpacted: 0, causes: {} });
  });

  for (const cycle of scanHistory) {
    const cycleScans = Array.isArray(cycle?.scans) ? cycle.scans : [];
    for (const scan of cycleScans) {
      const symbol = scan?.symbol;
      if (!symbol) continue;
      if (!rows.has(symbol)) {
        rows.set(symbol, { symbol, total: 0, iaConsulted: 0, infraImpacted: 0, causes: {} });
      }
      const row = rows.get(symbol);
      row.total += 1;
      if (scan?.ia_consulted) {
        row.iaConsulted += 1;
      }
      if (cycle?.balance_ok === false) {
        row.infraImpacted += 1;
      }
      const cause = scan?.rejection_reason || scan?.candidate_reason || "sin clasificar";
      row.causes[cause] = (row.causes[cause] || 0) + 1;
      causes.add(cause);
    }
  }

  const orderedCauses = Array.from(causes).sort((left, right) => {
    if (left === "sin escenario A ni B") return -1;
    if (right === "sin escenario A ni B") return 1;
    return left.localeCompare(right);
  });

  return {
    causes: orderedCauses,
    rows: Array.from(rows.values()).map((row) => ({
      ...row,
      iaConsultRate: row.total ? row.iaConsulted / row.total : 0,
      causeRates: orderedCauses.reduce((acc, cause) => {
        acc[cause] = row.total ? (row.causes[cause] || 0) / row.total : 0;
        return acc;
      }, {}),
    })),
  };
}


function buildMatrixHighlights(matrixRows) {
  const withData = matrixRows.filter((row) => row.total > 0);
  const totalEvaluations = withData.reduce((sum, row) => sum + row.total, 0);
  const totalIaConsults = withData.reduce((sum, row) => sum + row.iaConsulted, 0);
  const totalInfraImpacted = withData.reduce((sum, row) => sum + row.infraImpacted, 0);
  const totalNoSetup = withData.reduce((sum, row) => sum + (row.causes["sin escenario A ni B"] || 0), 0);

  return {
    totalEvaluations,
    totalIaConsults,
    totalInfraImpacted,
    noSetupRate: totalEvaluations ? totalNoSetup / totalEvaluations : 0,
    iaRate: totalEvaluations ? totalIaConsults / totalEvaluations : 0,
    infraRate: totalEvaluations ? totalInfraImpacted / totalEvaluations : 0,
  };
}


function buildInfraBreakdown(scanHistory) {
  const buckets = {
    rate_limit: 0,
    timeout_binance: 0,
    network_local: 0,
    exchange_other: 0,
  };

  for (const cycle of scanHistory) {
    if (cycle?.balance_ok !== false) continue;
    const key = cycle?.balance_error_class || "exchange_other";
    buckets[key] = (buckets[key] || 0) + 1;
  }

  const total = Object.values(buckets).reduce((sum, value) => sum + value, 0);
  return { buckets, total };
}


function formatDate(value) {
  if (!value) {
    return "sin dato";
  }

  return new Date(value).toLocaleString("es-ES", {
    hour12: false,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}


function MetricCard({ label, value, tone = "neutral", subvalue }) {
  return (
    <div className={`metric-card ${tone}`}>
      <span className="metric-label">{label}</span>
      <strong className="metric-value">{value}</strong>
      {subvalue ? <span className="metric-subvalue">{subvalue}</span> : null}
    </div>
  );
}


export default function RejectionMatrixClient({ initialData }) {
  const [payload, setPayload] = useState(initialData);
  const [analyticsWindowMinutes, setAnalyticsWindowMinutes] = useState(60);

  useEffect(() => {
    const intervalId = window.setInterval(async () => {
      const response = await fetch("/api/state", { cache: "no-store" });
      const next = await response.json();
      setPayload(next);
    }, 10000);
    return () => window.clearInterval(intervalId);
  }, []);

  const state = payload?.state || {};
  const status = payload?.status || {};
  const targetSymbols = state?.target_symbols || [];
  const scanHistory = payload?.scanHistory || [];
  const rollingScans = useMemo(() => getRollingWindow(scanHistory, analyticsWindowMinutes), [scanHistory, analyticsWindowMinutes]);
  const rejectMatrix = useMemo(() => buildRejectMatrix(rollingScans, targetSymbols), [rollingScans, targetSymbols]);
  const matrixHighlights = useMemo(() => buildMatrixHighlights(rejectMatrix.rows), [rejectMatrix]);
  const infraBreakdown = useMemo(() => buildInfraBreakdown(rollingScans), [rollingScans]);

  return (
    <main className="matrix-page-shell">
      <header className="matrix-page-hero panel analytics-panel">
        <div>
          <p className="eyebrow">Analytics Lab</p>
          <h1 className="matrix-page-title">Matriz institucional de rechazo</h1>
          <p className="matrix-page-subtitle">Ventana rolling por ticker, causa, paso a IA e impacto de infraestructura.</p>
        </div>
        <div className="matrix-page-actions">
          <div className="timestamp">Heartbeat: {formatDate(status?.heartbeat_at)}</div>
          <div className="nav-pills">
            <Link href="/" className="nav-pill">Panel</Link>
            <Link href="/matriz" className="nav-pill active">Matriz</Link>
          </div>
        </div>
      </header>

      <section className="panel analytics-panel">
        <div className="panel-header">
          <div>
            <h3>Matriz de rechazo</h3>
            <span>Conteo por ticker, porcentaje por causa y tasa de paso a IA</span>
          </div>
          <span>
            <select
              value={analyticsWindowMinutes}
              onChange={(e) => setAnalyticsWindowMinutes(Number(e.target.value))}
              style={{ background: "transparent", color: "inherit", border: "1px solid #2a3744", borderRadius: 4, padding: "2px 6px", font: "inherit" }}
            >
              <option value={15}>15m</option>
              <option value={30}>30m</option>
              <option value={60}>60m</option>
              <option value={120}>120m</option>
            </select>
          </span>
        </div>
        <div className="analytics-kpis">
          <MetricCard label="Evaluaciones" value={String(matrixHighlights.totalEvaluations)} subvalue={`${rejectMatrix.rows.length} tickers`} />
          <MetricCard label="No setup" value={formatPercent(matrixHighlights.noSetupRate)} tone={matrixHighlights.noSetupRate >= 0.75 ? "warn" : "neutral"} subvalue="sin escenario A/B" />
          <MetricCard label="Paso a IA" value={formatPercent(matrixHighlights.iaRate)} tone={matrixHighlights.iaRate > 0 ? "buy" : "sell"} subvalue={`${matrixHighlights.totalIaConsults} consultas`} />
          <MetricCard label="Infra degradada" value={formatPercent(matrixHighlights.infraRate)} tone={matrixHighlights.infraRate > 0 ? "sell" : "buy"} subvalue={`${matrixHighlights.totalInfraImpacted} ciclos`} />
        </div>
        <div className="matrix-layout">
          <div className="table-wrap matrix-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Ticker</th>
                  <th>Eval.</th>
                  <th>Paso IA</th>
                  {rejectMatrix.causes.map((cause) => <th key={cause}>{cause}</th>)}
                </tr>
              </thead>
              <tbody>
                {rejectMatrix.rows.length === 0 ? <tr><td colSpan={3 + rejectMatrix.causes.length} className="empty">Sin historial rolling suficiente todavía.</td></tr> : rejectMatrix.rows.map((row) => (
                  <tr key={row.symbol}>
                    <td><strong>{row.symbol}</strong></td>
                    <td>{row.total}</td>
                    <td className={row.iaConsultRate > 0 ? "tone-buy" : "tone-sell"}>{formatPercent(row.iaConsultRate)}</td>
                    {rejectMatrix.causes.map((cause) => (
                      <td key={`${row.symbol}-${cause}`}>
                        <div className="matrix-cell">
                          <strong>{row.causes[cause] || 0}</strong>
                          <span>{formatPercent(row.causeRates[cause])}</span>
                        </div>
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="narrative-card compact matrix-callout">
            <strong>Lectura quirúrgica</strong>
            <p>
              {matrixHighlights.totalEvaluations === 0
                ? "Aún no hay una ventana rolling suficiente para clasificar rechazos por ticker."
                : matrixHighlights.noSetupRate >= 0.7
                  ? "La fricción dominante está antes de la IA: el universo no está construyendo Escenario A/B con suficiente frecuencia."
                  : "La fricción dominante ya no es solo el nacimiento de la señal; hay rechazo aguas abajo en guardrails o infraestructura."}
            </p>
            <p>
              {matrixHighlights.iaRate === 0
                ? "Ningún ticker está llegando a consulta IA en la ventana actual."
                : `La IA está siendo consultada en ${formatPercent(matrixHighlights.iaRate)} de las evaluaciones agregadas.`}
            </p>
            <p>
              {matrixHighlights.infraRate === 0
                ? "La ventana seleccionada no presenta ciclos impactados por degradación de balance."
                : "La matriz ya está separando ciclos con degradación de infraestructura para no mezclarlos con rechazo técnico puro."}
            </p>
          </div>
        </div>
      </section>

      <section className="panel analytics-panel">
        <div className="panel-header">
          <div>
            <h3>Taxonomía de errores</h3>
            <span>Diferenciación operativa entre limitación del exchange y fallos de red</span>
          </div>
          <span>{infraBreakdown.total} ciclos degradados clasificados</span>
        </div>
        <div className="analytics-kpis">
          <MetricCard label="Rate limit" value={String(infraBreakdown.buckets.rate_limit || 0)} tone={(infraBreakdown.buckets.rate_limit || 0) > 0 ? "warn" : "neutral"} subvalue={formatPercent(infraBreakdown.total ? (infraBreakdown.buckets.rate_limit || 0) / infraBreakdown.total : 0)} />
          <MetricCard label="Timeout Binance" value={String(infraBreakdown.buckets.timeout_binance || 0)} tone={(infraBreakdown.buckets.timeout_binance || 0) > 0 ? "sell" : "neutral"} subvalue={formatPercent(infraBreakdown.total ? (infraBreakdown.buckets.timeout_binance || 0) / infraBreakdown.total : 0)} />
          <MetricCard label="Network local" value={String(infraBreakdown.buckets.network_local || 0)} tone={(infraBreakdown.buckets.network_local || 0) > 0 ? "sell" : "neutral"} subvalue={formatPercent(infraBreakdown.total ? (infraBreakdown.buckets.network_local || 0) / infraBreakdown.total : 0)} />
          <MetricCard label="Exchange other" value={String(infraBreakdown.buckets.exchange_other || 0)} tone={(infraBreakdown.buckets.exchange_other || 0) > 0 ? "warn" : "neutral"} subvalue={formatPercent(infraBreakdown.total ? (infraBreakdown.buckets.exchange_other || 0) / infraBreakdown.total : 0)} />
        </div>
        <div className="narrative-card compact">
          <strong>Lectura de infraestructura</strong>
          <p>
            `rate_limit` implica presión excesiva contra Binance. `timeout_binance` apunta a latencia o saturación del exchange. `network_local` señala problemas de conectividad, DNS, SSL o transporte desde tu lado. `exchange_other` captura rechazos del exchange que no encajan en las tres clases primarias.
          </p>
        </div>
      </section>
    </main>
  );
}