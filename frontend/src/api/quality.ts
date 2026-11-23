/** 质量诊断扩展接口：历史报告、日志证据、对照筛选、复算明细与可复查趋势。 */
import { request } from './client'

export type QualityEvidence = {
  evidence_id?: string
  source_key?: string
  raw_log_file?: string
  snapshot_path?: string | null
  snapshot_exists?: boolean
  line_range?: { start: number; end: number; count: number } | null
  offset_range?: { start: number; end: number } | null
  content_digest?: string
  record_count?: number
  complete?: boolean
  reason?: string
  lines?: Record<string, unknown>[]
  report_id?: string
  rule_version?: string | null
  digest_verified?: boolean
}

export type QualityReportSummary = {
  report_id: string
  source_key: string
  diagnosed_at: number
  report_status: string
  verified: boolean
  parse_success_rate: number | null
  original_metrics: {
    parse_success_rate: number | null
    parsed_ok?: number | null
    parse_errors?: number | null
    total_records?: number | null
    field_completeness?: number | null
  }
  rule_version: string | null
  version_state: 'known' | 'unknown'
  version_reason?: string
  applied_rule_codes: string[]
  log_evidence: QualityEvidence
}

export type QualityReportList = {
  items: QualityReportSummary[]
  total: number
  original_values: {
    report_id: string
    source_key: string
    original_metrics: QualityReportSummary['original_metrics']
    rule_version: string | null
    version_state: 'known' | 'unknown'
  }[]
  filters: Record<string, string | null>
}

export type QualityComparison = {
  ok: boolean
  message: string
  source_key: string
  comparison_rule_version: string | null
  reason: string
  comparable: boolean
  requires_recompute: string[]
  reports: QualityReportSummary[]
  original_values: Record<string, QualityReportSummary['original_metrics']>
}

export type RecomputeDetail = {
  detail_id: string
  report_id: string
  source_key: string
  status: string
  verified: boolean
  interrupted: boolean
  reason: string
  rule_version: string | null
  original_value: number | null
  recomputed_value: number | null
  counts: Record<string, number>
  evidence: QualityEvidence
  provenance: Record<string, unknown>
  created_at: number
}

export type QualityTrendPoint = {
  report_id: string
  source_key: string
  rule_version: string
  value: number
  value_origin: 'original' | 'recomputed'
  verified: boolean
  original_value: number | null
  recomputed_value?: number | null
  detail_id?: string
  diagnosed_at: number
  evidence: QualityEvidence
  provenance: Record<string, unknown>
}

export type QualityTrend = {
  source_key: string
  rule_version: string | null
  status: string
  points: QualityTrendPoint[]
  all_points: QualityTrendPoint[]
  excluded: {
    report_id: string
    original_value: number | null
    rule_version: string | null
    version_state: string
    reason: string
    pending_status?: string | null
  }[]
  original_values: {
    report_id: string
    parse_success_rate: number | null
    rule_version: string | null
    version_state: string
  }[]
  latest_success_rate?: number | null
  previous_success_rate?: number | null
  trend?: 'up' | 'down' | 'stable' | 'insufficient'
  delta?: number | null
  groups?: Record<string, number>
}

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = payload && typeof payload.detail === 'string' ? payload.detail : fallback
    throw new Error(detail)
  }
  return payload as T
}

export async function listQualityReports(params: {
  source_key?: string
  rule_version?: string
  version_state?: string
}): Promise<QualityReportList> {
  const query = new URLSearchParams()
  if (params.source_key) query.set('source_key', params.source_key)
  if (params.rule_version) query.set('rule_version', params.rule_version)
  if (params.version_state) query.set('version_state', params.version_state)
  return readJson(await request(`/api/log_quality/reports?${query.toString()}`), '历史诊断查询失败')
}

export async function getQualityReport(reportId: string): Promise<Record<string, unknown>> {
  return readJson(await request(`/api/log_quality/reports/${encodeURIComponent(reportId)}`), '原报告读取失败')
}

export async function getQualityEvidence(
  reportId: string,
  includeLines = false,
): Promise<QualityEvidence> {
  const suffix = includeLines ? '?include_lines=true' : ''
  return readJson(
    await request(`/api/log_quality/reports/${encodeURIComponent(reportId)}/evidence${suffix}`),
    '原始日志证据读取失败',
  )
}

export async function listComparisonCandidates(params: {
  source_key?: string
  rule_version?: string
}): Promise<{ items: QualityComparison[]; total: number }> {
  const query = new URLSearchParams()
  if (params.source_key) query.set('source_key', params.source_key)
  if (params.rule_version) query.set('rule_version', params.rule_version)
  return readJson(
    await request(`/api/log_quality/comparison-candidates?${query.toString()}`),
    '对照筛选读取失败',
  )
}

export async function prepareComparison(
  reportIds: string[],
  ruleVersion?: string,
): Promise<QualityComparison> {
  return readJson(
    await request('/api/log_quality/comparison/prepare', {
      method: 'POST',
      body: JSON.stringify({ report_ids: reportIds, rule_version: ruleVersion || null }),
    }),
    '对照核对失败',
  )
}

export async function recomputeReport(
  reportId: string,
  payload: {
    rule_version?: string
    rule_code?: string
    peer_report_id?: string
    interrupt_after?: number | null
  },
): Promise<RecomputeDetail> {
  return readJson(
    await request(`/api/log_quality/reports/${encodeURIComponent(reportId)}/recompute`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
    '复算失败',
  )
}

export async function listRecomputeDetails(params: {
  report_id?: string
  source_key?: string
}): Promise<{ items: RecomputeDetail[]; total: number }> {
  const query = new URLSearchParams()
  if (params.report_id) query.set('report_id', params.report_id)
  if (params.source_key) query.set('source_key', params.source_key)
  return readJson(await request(`/api/log_quality/recompute-details?${query.toString()}`), '复算明细读取失败')
}

export async function loadQualityTrend(params: {
  source_key: string
  rule_version?: string
  report_ids?: string[]
}): Promise<QualityTrend> {
  const query = new URLSearchParams({ source_key: params.source_key })
  if (params.rule_version) query.set('rule_version', params.rule_version)
  if (params.report_ids?.length) query.set('report_ids', params.report_ids.join(','))
  return readJson(await request(`/api/log_quality/trend?${query.toString()}`), '趋势读取失败')
}
