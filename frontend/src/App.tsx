import { useCallback, useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import './App.css'

type SecretStatus = {
  configured: boolean
  masked?: string | null
}

type ConfigStatus = {
  secrets: Record<string, SecretStatus>
  runtime: Record<string, Record<string, unknown>>
}

type AuditItem = {
  action: string
  config_type: string
  config_name: string
  actor: string
  actor_ip?: string | null
  timestamp: string
  details?: Record<string, unknown> | null
}

type AuditResponse = {
  items: AuditItem[]
}

type SaveState = 'idle' | 'loading' | 'success' | 'failure'
type WindStatusText = '未配置' | '已配置 · 待测试' | '测试通过' | '测试失败' | '需要轮换'

type WindForm = {
  cli_path: string
  timeout_seconds: string
  retry_count: string
  backoff_seconds: string
}

type SchedulerForm = {
  cron_daily: string
  manual_rerun_enabled: boolean
  force_confirm_required: boolean
  publish_channel: string
  markdown_output_dir: string
}

// Snapshot rows returned by GET /api/v1/etf/snapshots. Distinct from
// `EtfRow` below — that's the editable pool config; this is read-only
// daily-fetch output.
type EtfSnapshot = {
  windcode: string
  name: string | null
  trade_date: string
  fund_size_yuan: number | null
  nav: number | null
  cumulative_nav: number | null
  change_range: number | null
  iopv: number | null
  forward_discount: number | null
  shares: number | null
  shares_status: 'VALID' | 'INVALID' | 'MISSING' | 'NOT_APPLICABLE'
  missing_reason: string | null
  data_source_version: string
}

type SnapshotsResponse = {
  trade_date: string
  rows: EtfSnapshot[]
  data_source_versions: string[]
}

type EtfRow = {
  windcode: string
  display_name: string
  bucket: string
  index_or_theme: string
  is_active: boolean
  start_date: string
  notes: string
}

type ThresholdForm = {
  scale_surge_pct: string
  scale_surge_amount_cny: string
  scale_drop_pct: string
  scale_drop_amount_cny: string
  consecutive_flow_days: string
  invalid_policy: string
}

type ModelForm = {
  mode: string
  llm_provider: string
  llm_model: string
}

const GROUPS = [
  'Wind 数据源',
  '调度与日报发布',
  '重点 ETF 池',
  '阈值与口径',
  '模型与结论生成',
  '系统安全与审计',
] as const

const API_BASE = '/api/v1'

const DEFAULT_ETF_ROWS: EtfRow[] = [
  {
    windcode: '510300.SH',
    display_name: '沪深300ETF',
    bucket: 'broad',
    index_or_theme: '沪深300',
    is_active: true,
    start_date: '2026-05-11',
    notes: '',
  },
]

const numberOrText = (value: string) => {
  const trimmed = value.trim()
  if (trimmed === '') {
    return ''
  }
  const parsed = Number(trimmed)
  return Number.isFinite(parsed) ? parsed : trimmed
}

const runtimeString = (
  runtime: ConfigStatus['runtime'],
  section: string,
  key: string,
  fallback: string,
) => {
  const value = runtime[section]?.[key]
  if (typeof value === 'string') {
    return value
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  return fallback
}

const runtimeBool = (
  runtime: ConfigStatus['runtime'],
  section: string,
  key: string,
  fallback: boolean,
) => {
  const value = runtime[section]?.[key]
  return typeof value === 'boolean' ? value : fallback
}

const runtimeEtfs = (runtime: ConfigStatus['runtime']) => {
  const value = runtime.etf_pool?.items
  if (!Array.isArray(value)) {
    return DEFAULT_ETF_ROWS
  }
  return value.map((item) => {
    const row = item as Partial<Record<keyof EtfRow, unknown>>
    return {
      windcode: typeof row.windcode === 'string' ? row.windcode : '',
      display_name: typeof row.display_name === 'string' ? row.display_name : '',
      bucket: typeof row.bucket === 'string' ? row.bucket : '',
      index_or_theme:
        typeof row.index_or_theme === 'string' ? row.index_or_theme : '',
      is_active: typeof row.is_active === 'boolean' ? row.is_active : false,
      start_date: typeof row.start_date === 'string' ? row.start_date : '',
      notes: typeof row.notes === 'string' ? row.notes : '',
    }
  })
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  })
  if (response.status === 401 || response.status === 403) {
    throw new Error('需要管理员权限')
  }
  if (!response.ok) {
    throw new Error('请求失败，请检查配置后重试')
  }
  return (await response.json()) as T
}

function buildForms(status: ConfigStatus) {
  const { runtime } = status
  return {
    wind: {
      cli_path: runtimeString(runtime, 'wind', 'cli_path', 'node'),
      timeout_seconds: runtimeString(runtime, 'wind', 'timeout_seconds', '20'),
      retry_count: runtimeString(runtime, 'wind', 'retry_count', '2'),
      backoff_seconds: runtimeString(runtime, 'wind', 'backoff_seconds', '5'),
    },
    scheduler: {
      cron_daily: runtimeString(
        runtime,
        'scheduler',
        'cron_daily',
        '0 18 * * MON-FRI',
      ),
      manual_rerun_enabled: runtimeBool(
        runtime,
        'scheduler',
        'manual_rerun_enabled',
        false,
      ),
      force_confirm_required: runtimeBool(
        runtime,
        'scheduler',
        'force_confirm_required',
        true,
      ),
      publish_channel: runtimeString(
        runtime,
        'scheduler',
        'publish_channel',
        '#基金数据每日汇报',
      ),
      markdown_output_dir: runtimeString(
        runtime,
        'scheduler',
        'markdown_output_dir',
        'reports/daily',
      ),
    },
    etfs: runtimeEtfs(runtime),
    thresholds: {
      scale_surge_pct: runtimeString(
        runtime,
        'thresholds',
        'scale_surge_pct',
        '10',
      ),
      scale_surge_amount_cny: runtimeString(
        runtime,
        'thresholds',
        'scale_surge_amount_cny',
        '5',
      ),
      scale_drop_pct: runtimeString(runtime, 'thresholds', 'scale_drop_pct', '-10'),
      scale_drop_amount_cny: runtimeString(
        runtime,
        'thresholds',
        'scale_drop_amount_cny',
        '-5',
      ),
      consecutive_flow_days: runtimeString(
        runtime,
        'thresholds',
        'consecutive_flow_days',
        '5',
      ),
      invalid_policy:
        'INVALID/MISSING/NOT_APPLICABLE 不落 0；份额数据不可用时不打连续流入/流出标签',
    },
    model: {
      mode: runtimeString(runtime, 'model', 'mode', 'rules_only'),
      llm_provider: runtimeString(runtime, 'model', 'llm_provider', ''),
      llm_model: runtimeString(runtime, 'model', 'llm_model', ''),
    },
  }
}

function App() {
  const [status, setStatus] = useState<ConfigStatus | null>(null)
  const [auditItems, setAuditItems] = useState<AuditItem[]>([])
  const [activeGroup, setActiveGroup] = useState<(typeof GROUPS)[number]>(GROUPS[0])
  const [windForm, setWindForm] = useState<WindForm>({
    cli_path: 'node',
    timeout_seconds: '20',
    retry_count: '2',
    backoff_seconds: '5',
  })
  const [schedulerForm, setSchedulerForm] = useState<SchedulerForm>({
    cron_daily: '0 18 * * MON-FRI',
    manual_rerun_enabled: false,
    force_confirm_required: true,
    publish_channel: '#基金数据每日汇报',
    markdown_output_dir: 'reports/daily',
  })
  const [etfs, setEtfs] = useState<EtfRow[]>(DEFAULT_ETF_ROWS)
  const [thresholds, setThresholds] = useState<ThresholdForm>({
    scale_surge_pct: '10',
    scale_surge_amount_cny: '5',
    scale_drop_pct: '-10',
    scale_drop_amount_cny: '-5',
    consecutive_flow_days: '5',
    invalid_policy:
      'INVALID/MISSING/NOT_APPLICABLE 不落 0；份额数据不可用时不打连续流入/流出标签',
  })
  const [modelForm, setModelForm] = useState<ModelForm>({
    mode: 'rules_only',
    llm_provider: '',
    llm_model: '',
  })
  const [secretDraft, setSecretDraft] = useState('')
  const [showSecretDraft, setShowSecretDraft] = useState(false)
  const [windTest, setWindTest] = useState<{
    status: WindStatusText
    latency_ms?: number
    checked_at?: string
    message: string
  }>({ status: '未配置', message: '配置 Key 后可测试连接' })
  const [saveState, setSaveState] = useState<Record<string, SaveState>>({})
  const [authError, setAuthError] = useState('')
  const [loadError, setLoadError] = useState('')
  // ETF snapshots — the "运行我看看" surface. Loaded after auth alongside
  // config so the dashboard's first paint shows real data when present.
  const [snapshots, setSnapshots] = useState<SnapshotsResponse | null>(null)
  const [snapshotsError, setSnapshotsError] = useState('')

  const windSecret = status?.secrets.wind_api_key
  const secretStatusText = windSecret?.configured
    ? `已配置 · ${windSecret.masked ?? '****'}`
    : '未配置'
  const dataSourceStatus: WindStatusText = windSecret?.configured
    ? windTest.status === '未配置'
      ? '已配置 · 待测试'
      : windTest.status
    : '未配置'
  const versionLabel = useMemo(() => {
    const firstAudit = auditItems[0]
    return firstAudit ? `v${auditItems.length}` : 'v0'
  }, [auditItems])

  const loadConfig = useCallback(async () => {
    setLoadError('')
    try {
      const [nextStatus, audit] = await Promise.all([
        requestJson<ConfigStatus>('/config/status'),
        requestJson<AuditResponse>('/config/audit'),
      ])
      setStatus(nextStatus)
      setAuditItems(audit.items)
      const forms = buildForms(nextStatus)
      setWindForm(forms.wind)
      setSchedulerForm(forms.scheduler)
      setEtfs(forms.etfs)
      setThresholds(forms.thresholds)
      setModelForm(forms.model)
      setAuthError('')
    } catch (error) {
      const message = error instanceof Error ? error.message : '加载失败'
      if (message === '需要管理员权限') {
        setAuthError(message)
      } else {
        setLoadError(message)
      }
    }
  }, [])

  // Separate loader so a snapshot fetch failure (e.g. DB empty before
  // first run) doesn't blow up the config-page paint.
  const loadSnapshots = useCallback(async () => {
    setSnapshotsError('')
    try {
      const data = await requestJson<SnapshotsResponse>('/etf/snapshots')
      setSnapshots(data)
    } catch (error) {
      const message = error instanceof Error ? error.message : '快照加载失败'
      if (message !== '需要管理员权限') {
        setSnapshotsError(message)
      }
    }
  }, [])

  useEffect(() => {
    void loadConfig()
    void loadSnapshots()
  }, [loadConfig, loadSnapshots])

  async function saveSection(section: string, values: Record<string, unknown>) {
    setSaveState((current) => ({ ...current, [section]: 'loading' }))
    try {
      await requestJson(`/config/sections/${section}`, {
        method: 'PUT',
        body: JSON.stringify({ values }),
      })
      setSaveState((current) => ({ ...current, [section]: 'success' }))
      await loadConfig()
    } catch {
      setSaveState((current) => ({ ...current, [section]: 'failure' }))
    }
  }

  async function saveWindSecret(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!secretDraft.trim()) {
      return
    }
    if (!window.confirm('替换 Key 后旧值不可恢复，确认继续？')) {
      return
    }
    setSaveState((current) => ({ ...current, wind_secret: 'loading' }))
    try {
      const result = await requestJson<{ masked: string }>(
        '/config/secrets/wind_api_key',
        {
          method: 'PUT',
          body: JSON.stringify({ value: secretDraft }),
        },
      )
      setSecretDraft('')
      setShowSecretDraft(false)
      setStatus((current) =>
        current
          ? {
              ...current,
              secrets: {
                ...current.secrets,
                wind_api_key: { configured: true, masked: result.masked },
              },
            }
          : current,
      )
      setWindTest({ status: '已配置 · 待测试', message: '检查 key 或点击测试连接' })
      setSaveState((current) => ({ ...current, wind_secret: 'success' }))
    } catch {
      setSaveState((current) => ({ ...current, wind_secret: 'failure' }))
    }
  }

  async function deleteWindSecret() {
    if (!window.confirm('删除 Key 后旧值不可恢复，确认继续？')) {
      return
    }
    setSaveState((current) => ({ ...current, delete_secret: 'loading' }))
    try {
      await requestJson('/config/secrets/wind_api_key', { method: 'DELETE' })
      setSecretDraft('')
      setStatus((current) =>
        current
          ? {
              ...current,
              secrets: {
                ...current.secrets,
                wind_api_key: { configured: false, masked: null },
              },
            }
          : current,
      )
      setWindTest({ status: '未配置', message: '配置 Key 后可测试连接' })
      setSaveState((current) => ({ ...current, delete_secret: 'success' }))
    } catch {
      setSaveState((current) => ({ ...current, delete_secret: 'failure' }))
    }
  }

  async function testWindConnection() {
    setSaveState((current) => ({ ...current, test_wind: 'loading' }))
    try {
      const result = await requestJson<{ status: 'ok'; latency_ms: number }>(
        '/config/test/wind',
        { method: 'POST' },
      )
      setWindTest({
        status: '测试通过',
        latency_ms: result.latency_ms,
        checked_at: new Date().toLocaleString('zh-CN'),
        message: '连接正常',
      })
      setSaveState((current) => ({ ...current, test_wind: 'success' }))
    } catch {
      setWindTest({
        status: '测试失败',
        message: '检查 key 或点击替换',
        checked_at: new Date().toLocaleString('zh-CN'),
      })
      setSaveState((current) => ({ ...current, test_wind: 'failure' }))
    }
  }

  function updateEtf(index: number, patch: Partial<EtfRow>) {
    setEtfs((current) =>
      current.map((row, rowIndex) =>
        rowIndex === index ? { ...row, ...patch } : row,
      ),
    )
  }

  function deactivateEtf(index: number) {
    updateEtf(index, { is_active: false })
  }

  function addEtf() {
    setEtfs((current) => [
      ...current,
      {
        windcode: '',
        display_name: '',
        bucket: '',
        index_or_theme: '',
        is_active: true,
        start_date: '',
        notes: '',
      },
    ])
  }

  const latestAudit = auditItems[0]

  return (
    <main className="app-shell">
      <nav className="top-nav" aria-label="主导航">
        <span className="brand">funds-dashboard</span>
        <a href="#config" aria-current="page">
          系统配置
        </a>
      </nav>

      <header className="page-header" id="config">
        <div>
          <p className="eyebrow">Phase 1</p>
          <h1>系统配置</h1>
        </div>
        <div className="status-strip" aria-label="全局配置状态">
          <StatusMetric label="配置版本" value={versionLabel} />
          <StatusMetric label="最近更新人" value={latestAudit?.actor ?? '未记录'} />
          <StatusMetric
            label="最近更新时间"
            value={
              latestAudit
                ? new Date(latestAudit.timestamp).toLocaleString('zh-CN')
                : '未记录'
            }
          />
          <StatusMetric label="Wind 连接状态" value={windTest.status} />
          <StatusMetric label="当前数据源状态" value={dataSourceStatus} />
        </div>
      </header>

      {authError ? <div className="notice blocked">需要管理员权限</div> : null}
      {loadError ? <div className="notice failed">{loadError}</div> : null}

      <EtfSnapshotsPanel
        data={snapshots}
        error={snapshotsError}
        onRefresh={() => {
          void loadSnapshots()
        }}
      />


      <div className="config-layout">
        <aside className="group-nav" aria-label="配置分组">
          {GROUPS.map((group) => (
            <button
              className={activeGroup === group ? 'active' : ''}
              key={group}
              type="button"
              onClick={() => setActiveGroup(group)}
            >
              {group}
            </button>
          ))}
        </aside>

        <section className="config-sections" aria-live="polite">
          <ConfigSection
            active={activeGroup === 'Wind 数据源'}
            title="Wind 数据源"
          >
            <div className="section-grid">
              <div className="field-stack">
                <span className="field-label">Key 状态</span>
                <strong>{secretStatusText}</strong>
                <p className="hint">保存后只显示 masked 后 4 位，不提供查看明文。</p>
              </div>
              <div className="field-stack">
                <span className="field-label">最近测试结果</span>
                <strong>{windTest.status}</strong>
                <p className="hint">{windTest.message}</p>
              </div>
            </div>

            <form className="form-row" onSubmit={saveWindSecret}>
              <label>
                新 Wind API Key
                <input
                  autoComplete="off"
                  type={showSecretDraft ? 'text' : 'password'}
                  value={secretDraft}
                  onChange={(event) => setSecretDraft(event.target.value)}
                  placeholder="保存后输入框会清空"
                />
              </label>
              <button type="button" onClick={() => setShowSecretDraft((value) => !value)}>
                {showSecretDraft ? '隐藏输入' : '显示输入'}
              </button>
              <button disabled={saveState.wind_secret === 'loading'} type="submit">
                替换 Key
              </button>
              <button
                disabled={saveState.delete_secret === 'loading'}
                type="button"
                onClick={deleteWindSecret}
              >
                删除 Key
              </button>
            </form>
            <div className="secret-action-status" aria-live="polite">
              <OperationStatus
                state={saveState.wind_secret}
                labels={{
                  loading: '保存中',
                  success: '已替换',
                  failure: '替换失败，请重试',
                }}
              />
              <OperationStatus
                state={saveState.delete_secret}
                labels={{
                  loading: '删除中',
                  success: '已删除',
                  failure: '删除失败，请重试',
                }}
              />
            </div>

            <div className="form-grid">
              <label>
                CLI path
                <input
                  value={windForm.cli_path}
                  onChange={(event) =>
                    setWindForm((current) => ({
                      ...current,
                      cli_path: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                Timeout
                <input
                  value={windForm.timeout_seconds}
                  onChange={(event) =>
                    setWindForm((current) => ({
                      ...current,
                      timeout_seconds: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                Retry
                <input
                  value={windForm.retry_count}
                  onChange={(event) =>
                    setWindForm((current) => ({
                      ...current,
                      retry_count: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                Backoff
                <input
                  value={windForm.backoff_seconds}
                  onChange={(event) =>
                    setWindForm((current) => ({
                      ...current,
                      backoff_seconds: event.target.value,
                    }))
                  }
                />
              </label>
            </div>
            <ActionBar state={saveState.wind}>
              <button
                type="button"
                onClick={() =>
                  saveSection('wind', {
                    cli_path: windForm.cli_path,
                    timeout_seconds: numberOrText(windForm.timeout_seconds),
                    retry_count: numberOrText(windForm.retry_count),
                    backoff_seconds: numberOrText(windForm.backoff_seconds),
                  })
                }
              >
                保存 Wind 参数
              </button>
              <button
                disabled={saveState.test_wind === 'loading' || !windSecret?.configured}
                type="button"
                onClick={testWindConnection}
              >
                测试连接
              </button>
            </ActionBar>
            {windTest.latency_ms !== undefined ? (
              <dl className="result-list">
                <div>
                  <dt>tool name</dt>
                  <dd>Wind CLI</dd>
                </div>
                <div>
                  <dt>latency</dt>
                  <dd>{windTest.latency_ms} ms</dd>
                </div>
                <div>
                  <dt>timestamp</dt>
                  <dd>{windTest.checked_at}</dd>
                </div>
              </dl>
            ) : null}
          </ConfigSection>

          <ConfigSection
            active={activeGroup === '调度与日报发布'}
            title="调度与日报发布"
          >
            <div className="form-grid">
              <label>
                cron
                <input
                  value={schedulerForm.cron_daily}
                  onChange={(event) =>
                    setSchedulerForm((current) => ({
                      ...current,
                      cron_daily: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                发布频道
                <input
                  value={schedulerForm.publish_channel}
                  onChange={(event) =>
                    setSchedulerForm((current) => ({
                      ...current,
                      publish_channel: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                markdown 输出目录
                <input
                  value={schedulerForm.markdown_output_dir}
                  onChange={(event) =>
                    setSchedulerForm((current) => ({
                      ...current,
                      markdown_output_dir: event.target.value,
                    }))
                  }
                />
              </label>
            </div>
            <div className="toggle-row">
              <label>
                <input
                  checked={schedulerForm.manual_rerun_enabled}
                  type="checkbox"
                  onChange={(event) =>
                    setSchedulerForm((current) => ({
                      ...current,
                      manual_rerun_enabled: event.target.checked,
                    }))
                  }
                />
                手动重跑
              </label>
              <label>
                <input
                  checked={schedulerForm.force_confirm_required}
                  type="checkbox"
                  onChange={(event) =>
                    setSchedulerForm((current) => ({
                      ...current,
                      force_confirm_required: event.target.checked,
                    }))
                  }
                />
                force confirm
              </label>
            </div>
            <ActionBar state={saveState.scheduler}>
              <button
                type="button"
                onClick={() => {
                  if (
                    window.confirm(
                      '改发布频道或 force rerun 需要确认，是否保存？',
                    )
                  ) {
                    void saveSection('scheduler', schedulerForm)
                  }
                }}
              >
                保存调度配置
              </button>
            </ActionBar>
          </ConfigSection>

          <ConfigSection active={activeGroup === '重点 ETF 池'} title="重点 ETF 池">
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>windcode</th>
                    <th>display_name</th>
                    <th>bucket</th>
                    <th>index_or_theme</th>
                    <th>is_active</th>
                    <th>start_date</th>
                    <th>notes</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {etfs.map((row, index) => (
                    <tr key={`${row.windcode}-${index}`}>
                      {(
                        [
                          'windcode',
                          'display_name',
                          'bucket',
                          'index_or_theme',
                          'start_date',
                          'notes',
                        ] as const
                      ).map((key) => (
                        <td key={key}>
                          <input
                            aria-label={`${key}-${index}`}
                            value={row[key]}
                            onChange={(event) =>
                              updateEtf(index, { [key]: event.target.value })
                            }
                          />
                        </td>
                      ))}
                      <td>
                        <input
                          aria-label={`is_active-${index}`}
                          checked={row.is_active}
                          type="checkbox"
                          onChange={(event) =>
                            updateEtf(index, { is_active: event.target.checked })
                          }
                        />
                      </td>
                      <td>
                        <button type="button" onClick={() => deactivateEtf(index)}>
                          停用
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <ActionBar state={saveState.etf_pool}>
              <button type="button" onClick={addEtf}>
                新增 ETF
              </button>
              <button type="button" onClick={() => saveSection('etf_pool', { items: etfs })}>
                保存 ETF 池
              </button>
            </ActionBar>
            <p className="hint">历史项只能停用，不直接删除。</p>
          </ConfigSection>

          <ConfigSection active={activeGroup === '阈值与口径'} title="阈值与口径">
            <div className="policy-lock">
              <strong>INVALID/MISSING/NOT_APPLICABLE 不落 0</strong>
              <span>份额数据不可用时不打连续流入/流出标签；前端不自行判断业务标签。</span>
            </div>
            <div className="form-grid">
              <label>
                scale_surge_pct（%）
                <input
                  value={thresholds.scale_surge_pct}
                  onChange={(event) =>
                    setThresholds((current) => ({
                      ...current,
                      scale_surge_pct: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                scale_surge_amount_cny（亿元）
                <input
                  value={thresholds.scale_surge_amount_cny}
                  onChange={(event) =>
                    setThresholds((current) => ({
                      ...current,
                      scale_surge_amount_cny: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                scale_drop_pct（%）
                <input
                  value={thresholds.scale_drop_pct}
                  onChange={(event) =>
                    setThresholds((current) => ({
                      ...current,
                      scale_drop_pct: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                scale_drop_amount_cny（亿元）
                <input
                  value={thresholds.scale_drop_amount_cny}
                  onChange={(event) =>
                    setThresholds((current) => ({
                      ...current,
                      scale_drop_amount_cny: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                consecutive_flow_days（天）
                <input
                  value={thresholds.consecutive_flow_days}
                  onChange={(event) =>
                    setThresholds((current) => ({
                      ...current,
                      consecutive_flow_days: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                固定口径
                <input readOnly value={thresholds.invalid_policy} />
              </label>
            </div>
            <ActionBar state={saveState.thresholds}>
              <button
                type="button"
                onClick={() =>
                  saveSection('thresholds', {
                    scale_surge_pct: numberOrText(thresholds.scale_surge_pct),
                    scale_surge_amount_cny: numberOrText(
                      thresholds.scale_surge_amount_cny,
                    ),
                    scale_drop_pct: numberOrText(thresholds.scale_drop_pct),
                    scale_drop_amount_cny: numberOrText(
                      thresholds.scale_drop_amount_cny,
                    ),
                    consecutive_flow_days: numberOrText(
                      thresholds.consecutive_flow_days,
                    ),
                    invalid_policy: thresholds.invalid_policy,
                  })
                }
              >
                保存阈值
              </button>
            </ActionBar>
          </ConfigSection>

          <ConfigSection
            active={activeGroup === '模型与结论生成'}
            title="模型与结论生成"
          >
            <div className="form-grid">
              <label>
                MVP 模式
                <select
                  value={modelForm.mode}
                  onChange={(event) =>
                    setModelForm((current) => ({
                      ...current,
                      mode: event.target.value,
                    }))
                  }
                >
                  <option value="rules_only">rules_only</option>
                </select>
              </label>
              <label>
                LLM provider
                <input disabled value={modelForm.llm_provider} placeholder="预留" />
              </label>
              <label>
                LLM model
                <input disabled value={modelForm.llm_model} placeholder="预留" />
              </label>
            </div>
            <p className="hint">LLM 不覆盖规则计算结果。</p>
            <ActionBar state={saveState.model}>
              <button type="button" onClick={() => saveSection('model', modelForm)}>
                保存模型配置
              </button>
            </ActionBar>
          </ConfigSection>

          <ConfigSection
            active={activeGroup === '系统安全与审计'}
            title="系统安全与审计"
          >
            <div className="audit-list" data-testid="audit-list">
              {auditItems.length === 0 ? (
                <p className="hint">暂无审计记录</p>
              ) : (
                auditItems.map((item) => (
                  <div className="audit-row" key={`${item.timestamp}-${item.config_name}`}>
                    <span>{item.config_name}</span>
                    <span>{item.action}</span>
                    <span>{item.actor}</span>
                    <time dateTime={item.timestamp}>
                      {new Date(item.timestamp).toLocaleString('zh-CN')}
                    </time>
                    <span>{item.details?.status === 'ok' ? 'ok' : '已记录'}</span>
                  </div>
                ))
              )}
            </div>
          </ConfigSection>
        </section>
      </div>
    </main>
  )
}

function StatusMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="status-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

// Read-only ETF snapshot table — shown at the top of the page so the
// first thing feng-lu / Linda / ops see is "did today's fetch land?"
// Renders missing-data cells as "—" with the explicit reason in a
// dedicated column, per Linda's no-coerce-to-0 rule (msg=91b45123)
// extended to the UI surface.
function EtfSnapshotsPanel({
  data,
  error,
  onRefresh,
}: {
  data: SnapshotsResponse | null
  error: string
  onRefresh: () => void
}) {
  const rowCount = data?.rows.length ?? 0
  return (
    <section className="etf-snapshots-panel">
      <header className="panel-header">
        <h2>今日 ETF 规模快照</h2>
        <div className="panel-meta">
          {data ? (
            <>
              <span>交易日：{data.trade_date}</span>
              {data.data_source_versions.length > 0 ? (
                <span title={data.data_source_versions.join(', ')}>
                  数据源版本：{data.data_source_versions.length} 个
                </span>
              ) : null}
            </>
          ) : null}
          <button type="button" onClick={onRefresh}>
            刷新
          </button>
        </div>
      </header>
      {error ? <div className="notice failed">{error}</div> : null}
      {rowCount === 0 ? (
        <p className="empty-state">
          当前交易日还没有数据。运行 <code>funds-dashboard-fetch --trade-date YYYY-MM-DD --force</code>{' '}
          后再刷新本页面。
        </p>
      ) : (
        <table className="etf-snapshot-table">
          <thead>
            <tr>
              <th>windcode</th>
              <th>简称</th>
              <th>基金规模 (亿元)</th>
              <th>份额 (份)</th>
              <th>份额状态</th>
              <th>缺失原因</th>
              <th>净值</th>
            </tr>
          </thead>
          <tbody>
            {data!.rows.map((r) => (
              <tr key={`${r.windcode}-${r.data_source_version}`}>
                <td>{r.windcode}</td>
                <td>{r.name ?? '—'}</td>
                <td>
                  {r.fund_size_yuan !== null
                    ? (r.fund_size_yuan / 1e8).toFixed(2)
                    : '—'}
                </td>
                <td>{r.shares !== null ? r.shares.toLocaleString() : '—'}</td>
                <td>
                  <span className={`status-pill status-${r.shares_status.toLowerCase()}`}>
                    {r.shares_status}
                  </span>
                </td>
                <td>{r.missing_reason ?? '—'}</td>
                <td>{r.nav !== null ? r.nav.toFixed(4) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="panel-note">
        缺失原因映射：<code>invalid_value</code> Wind 返回无效值；
        <code>not_returned</code> 字段未返回；
        <code>not_applicable</code> 标的不适用。数值列为 — 表示无效或未返回 — <strong>不是 0</strong>。
      </p>
    </section>
  )
}

function ConfigSection({
  active,
  title,
  children,
}: {
  active: boolean
  title: string
  children: React.ReactNode
}) {
  return (
    <section className={active ? 'config-section active' : 'config-section'}>
      <h2>{title}</h2>
      {children}
    </section>
  )
}

function ActionBar({
  state,
  children,
}: {
  state?: SaveState
  children: React.ReactNode
}) {
  return (
    <div className="action-bar">
      {children}
      {state === 'loading' ? <span>保存中</span> : null}
      {state === 'success' ? <span className="ok">已保存</span> : null}
      {state === 'failure' ? <span className="fail">保存失败，请重试</span> : null}
    </div>
  )
}

function OperationStatus({
  state,
  labels,
}: {
  state?: SaveState
  labels: Record<Exclude<SaveState, 'idle'>, string>
}) {
  if (!state || state === 'idle') {
    return null
  }
  return (
    <span className={state === 'failure' ? 'fail' : state === 'success' ? 'ok' : ''}>
      {labels[state]}
    </span>
  )
}

export default App
