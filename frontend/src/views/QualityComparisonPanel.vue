<template>
  <section class="quality-compare" data-testid="quality-compare-panel">
    <header class="page-head">
      <div>
        <h3>历史诊断对照与复算</h3>
        <p class="page-desc">
          按规则版本查询历史诊断，选出两份报告核对口径；缺版本报告只能用另一份明确版本补证复算，
          原值始终单独保留。
        </p>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="loadReports">
      <label class="filter-item">
        <span>关联源</span>
        <input v-model="filters.sourceKey" data-testid="quality-source" placeholder="例如 LOG_-0003" />
      </label>
      <label class="filter-item">
        <span>规则版本</span>
        <input v-model="filters.ruleVersion" data-testid="quality-rule-version" placeholder="例如 v1.2" />
      </label>
      <label class="filter-item">
        <span>版本状态</span>
        <select v-model="filters.versionState" data-testid="quality-version-state">
          <option value="all">全部</option>
          <option value="known">明确版本</option>
          <option value="unknown">缺版本</option>
        </select>
      </label>
      <button class="btn" data-testid="quality-query" type="submit">查询历史报告</button>
    </form>

    <p v-if="errorMessage" class="error-text" data-testid="quality-error">{{ errorMessage }}</p>

    <table class="data-table">
      <thead>
        <tr>
          <th>选择</th>
          <th>报告编号</th>
          <th>关联源</th>
          <th>规则版本</th>
          <th>版本状态</th>
          <th>解析成功率（原值）</th>
          <th>原日志行范围</th>
          <th>证据摘要</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="report in reports"
          :key="report.report_id"
          :data-testid="`quality-report-${report.report_id}`"
          :data-version-state="report.version_state"
        >
          <td>
            <input
              type="checkbox"
              :checked="selected.includes(report.report_id)"
              :data-testid="`quality-select-${report.report_id}`"
              @change="toggleSelect(report.report_id)"
            />
          </td>
          <td>{{ report.report_id }}</td>
          <td>{{ report.source_key }}</td>
          <td>{{ report.rule_version ?? '未标注' }}</td>
          <td>{{ report.version_state === 'known' ? '明确版本' : '缺版本' }}</td>
          <td data-testid="quality-original-rate">{{ report.original_metrics.parse_success_rate }}</td>
          <td>
            <template v-if="report.log_evidence.line_range">
              {{ report.log_evidence.line_range.start }}–{{ report.log_evidence.line_range.end }}
              （{{ report.log_evidence.line_range.count }} 行）
            </template>
            <template v-else>不可定位</template>
          </td>
          <td>{{ report.log_evidence.content_digest || '—' }}</td>
          <td class="row-actions">
            <button
              class="link"
              type="button"
              :data-testid="`quality-evidence-${report.report_id}`"
              @click="showEvidence(report)"
            >
              定位原日志
            </button>
          </td>
        </tr>
        <tr v-if="!reports.length">
          <td colspan="9" class="empty-state">暂无历史诊断报告，可先执行质量诊断</td>
        </tr>
      </tbody>
    </table>

    <div class="page-actions quality-actions">
      <button class="btn primary" data-testid="quality-compare" type="button" :disabled="selected.length !== 2" @click="runComparison">
        核对所选报告（{{ selected.length }}/2）
      </button>
      <span class="quality-hint">选择一份明确版本 + 一份缺版本的报告，可核对可比结论与补证复算。</span>
    </div>

    <section v-if="evidence" class="quality-block" data-testid="quality-evidence-panel">
      <h4>原日志定位 · {{ evidence.report_id }}</h4>
      <dl class="dag-detail">
        <dt>原始日志文件</dt>
        <dd>{{ evidence.raw_log_file }}</dd>
        <dt>证据快照</dt>
        <dd>{{ evidence.snapshot_path || '无' }}（{{ evidence.snapshot_exists ? '存在' : '缺失' }}）</dd>
        <dt>行号范围</dt>
        <dd>
          <template v-if="evidence.line_range">
            {{ evidence.line_range.start }} – {{ evidence.line_range.end }}（{{ evidence.line_range.count }} 行）
          </template>
          <template v-else>无</template>
        </dd>
        <dt>内容摘要</dt>
        <dd>{{ evidence.content_digest || '—' }}</dd>
      </dl>
      <pre v-if="evidence.lines?.length" class="timeline-log">{{ evidence.lines.map((line) => line.raw).join('\n') }}</pre>
      <p v-else class="quality-hint">证据快照中没有可展示的原始行。</p>
    </section>

    <section v-if="comparison" class="quality-block" data-testid="quality-comparison">
      <h4>对照核对结果</h4>
      <p data-testid="quality-comparison-reason">{{ comparison.reason }}</p>
      <dl class="dag-detail">
        <dt>对照口径版本</dt>
        <dd data-testid="quality-comparison-version">{{ comparison.comparison_rule_version ?? '未确定' }}</dd>
        <dt>可比结论</dt>
        <dd data-testid="quality-comparison-comparable">{{ comparison.comparable ? '可比' : '不可直接比较' }}</dd>
        <dt>需要补证复算</dt>
        <dd>{{ comparison.requires_recompute.length ? comparison.requires_recompute.join('、') : '无' }}</dd>
      </dl>
      <div v-for="report in comparison.reports" :key="report.report_id" class="quality-recompute-row">
        <div>
          <strong>{{ report.report_id }}</strong>
          <span class="quality-hint">
            原值 {{ report.original_metrics.parse_success_rate }} ·
            {{ report.version_state === 'known' ? `版本 ${report.rule_version}` : '缺版本，需补证' }}
          </span>
        </div>
        <button
          v-if="comparison.requires_recompute.includes(report.report_id) && comparison.comparison_rule_version"
          class="btn"
          type="button"
          :data-testid="`quality-recompute-${report.report_id}`"
          @click="runRecompute(report.report_id)"
        >
          按 {{ comparison.comparison_rule_version }} 复算
        </button>
      </div>
    </section>

    <section v-if="recomputes.length" class="quality-block" data-testid="quality-recomputes">
      <h4>复算明细</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>报告</th>
            <th>状态</th>
            <th>规则版本</th>
            <th>原值</th>
            <th>复算值</th>
            <th>证据摘要核对</th>
            <th>来源凭据</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="detail in recomputes" :key="detail.detail_id" :data-testid="`quality-detail-${detail.detail_id}`">
            <td>{{ detail.report_id }}</td>
            <td>{{ detail.status }}</td>
            <td>{{ detail.rule_version ?? '—' }}</td>
            <td>{{ detail.original_value }}</td>
            <td>{{ detail.recomputed_value ?? '—' }}</td>
            <td>{{ detail.evidence?.digest_verified ? '一致' : '待核' }}</td>
            <td class="quality-hint">
              {{ detail.evidence?.snapshot_path || '—' }}<br />
              {{ detail.evidence?.content_digest || '' }}
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="trend" class="quality-block" data-testid="quality-trend">
      <h4>可复查趋势</h4>
      <p data-testid="quality-trend-status">{{ trend.status }}</p>
      <div v-if="trend.trend" class="analysis-summary">
        <article class="analysis-card">
          <span class="stat-label">当前点</span>
          <strong>{{ trend.latest_success_rate }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">上一已验证点</span>
          <strong>{{ trend.previous_success_rate }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">趋势 / 差值</span>
          <strong>{{ trend.trend }} / {{ trend.delta }}</strong>
        </article>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>报告</th>
            <th>规则版本</th>
            <th>取值</th>
            <th>取值来源</th>
            <th>原值</th>
            <th>证据</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="point in trend.points" :key="point.report_id" :data-testid="`quality-point-${point.report_id}`">
            <td>{{ point.report_id }}</td>
            <td>{{ point.rule_version }}</td>
            <td>{{ point.value }}</td>
            <td>{{ point.value_origin === 'original' ? '原报告' : '复算取数' }}</td>
            <td>{{ point.original_value }}</td>
            <td>{{ point.evidence?.content_digest || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="trend.excluded.length" class="quality-excluded" data-testid="quality-excluded">
        <h5>未计入趋势（原值仍可单独查回）</h5>
        <ul>
          <li v-for="item in trend.excluded" :key="item.report_id">
            {{ item.report_id }}：原值 {{ item.original_value }}，{{ item.reason }}
          </li>
        </ul>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  getQualityEvidence,
  listQualityReports,
  listRecomputeDetails,
  loadQualityTrend,
  prepareComparison,
  recomputeReport,
} from '@/api/quality'
import type {
  QualityComparison,
  QualityEvidence,
  QualityReportSummary,
  QualityTrend,
  RecomputeDetail,
} from '@/api/quality'

const filters = ref({
  sourceKey: '',
  ruleVersion: '',
  versionState: 'all',
})
const reports = ref<QualityReportSummary[]>([])
const selected = ref<string[]>([])
const comparison = ref<QualityComparison | null>(null)
const evidence = ref<QualityEvidence | null>(null)
const recomputes = ref<RecomputeDetail[]>([])
const trend = ref<QualityTrend | null>(null)
const errorMessage = ref('')

function toggleSelect(reportId: string) {
  const index = selected.value.indexOf(reportId)
  if (index >= 0) {
    selected.value.splice(index, 1)
  } else if (selected.value.length < 2) {
    selected.value.push(reportId)
  } else {
    selected.value = [selected.value[1], reportId]
  }
}

async function loadReports() {
  errorMessage.value = ''
  try {
    const payload = await listQualityReports({
      source_key: filters.value.sourceKey || undefined,
      rule_version: filters.value.ruleVersion || undefined,
      version_state: filters.value.versionState,
    })
    reports.value = payload.items
    selected.value = []
    comparison.value = null
    const sourceKey = filters.value.sourceKey || payload.items[0]?.source_key
    if (!filters.value.sourceKey && sourceKey) {
      // 首次进入时聚焦到最近一份报告所在的源，避免跨源报告混排。
      filters.value.sourceKey = sourceKey
      const scoped = await listQualityReports({
        source_key: sourceKey,
        rule_version: filters.value.ruleVersion || undefined,
        version_state: filters.value.versionState,
      })
      reports.value = scoped.items
    }
    if (sourceKey) {
      recomputes.value = (await listRecomputeDetails({ source_key: sourceKey })).items
      trend.value = await loadQualityTrend({
        source_key: sourceKey,
        rule_version: filters.value.ruleVersion || undefined,
      })
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '历史诊断查询失败'
  }
}

async function showEvidence(report: QualityReportSummary) {
  errorMessage.value = ''
  try {
    evidence.value = await getQualityEvidence(report.report_id, true)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '原始日志定位失败'
  }
}

async function runComparison() {
  errorMessage.value = ''
  try {
    comparison.value = await prepareComparison(selected.value, filters.value.ruleVersion || undefined)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对照核对失败'
  }
}

async function runRecompute(reportId: string) {
  errorMessage.value = ''
  try {
    const peer = comparison.value?.reports.find((item) => item.report_id !== reportId)
    await recomputeReport(reportId, {
      rule_version: comparison.value?.comparison_rule_version || undefined,
      peer_report_id: peer?.report_id,
    })
    await refreshAfterRecompute()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复算失败'
  }
}

async function refreshAfterRecompute() {
  const sourceKey = filters.value.sourceKey || reports.value[0]?.source_key
  if (!sourceKey) return
  recomputes.value = (await listRecomputeDetails({ source_key: sourceKey })).items
  trend.value = await loadQualityTrend({
    source_key: sourceKey,
    rule_version: filters.value.ruleVersion || undefined,
  })
}

onMounted(loadReports)
</script>

<style scoped>
.quality-compare { margin-top: 24px; border-top: 2px solid var(--border); padding-top: 16px; }
.quality-compare h3 { margin: 0; }
.quality-block { margin-top: 16px; padding: 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; }
.quality-block h4 { margin: 0 0 8px; }
.quality-block h5 { margin: 12px 0 6px; }
.quality-actions { margin-top: 10px; align-items: center; }
.quality-hint { color: var(--muted); font-size: 12px; }
.quality-recompute-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 6px 0; border-top: 1px solid #f0f2f5; }
.quality-excluded { margin-top: 10px; color: #b42318; font-size: 13px; }
.quality-excluded ul { margin: 4px 0 0; padding-left: 18px; }
</style>
