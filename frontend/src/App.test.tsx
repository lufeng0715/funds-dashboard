import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const apiSecret = 'ak_written_by_admin_SHOULD_NOT_RENDER'

function mockJson(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as Response)
}

function handleConfigFetch(input: RequestInfo | URL, init?: RequestInit) {
  const url = String(input)
  const method = init?.method ?? 'GET'

  if (url.endsWith('/api/v1/etf/snapshots')) {
    return mockJson({
      trade_date: '2026-05-11',
      data_source_versions: ['20260511#20260512T023410Z#demo'],
      rows: [
        {
          windcode: '510300.SH',
          name: '华泰柏瑞沪深300ETF',
          trade_date: '2026-05-11',
          fund_size_yuan: 168659650000,
          nav: null,
          cumulative_nav: null,
          change_range: null,
          iopv: null,
          forward_discount: null,
          shares: null,
          shares_status: 'MISSING',
          missing_reason: 'not_returned',
          data_source_version: '20260511#20260512T023410Z#demo',
        },
      ],
    })
  }

  if (url.endsWith('/api/v1/config/status')) {
    return mockJson({
      secrets: {
        wind_api_key: { configured: true, masked: '****7890' },
      },
      runtime: {
        wind: {
          cli_path: '/usr/local/bin/node',
          timeout_seconds: 20,
          retry_count: 2,
          backoff_seconds: 5,
        },
        scheduler: {
          cron_daily: '0 18 * * MON-FRI',
          manual_rerun_enabled: true,
          force_confirm_required: true,
          publish_channel: '#基金数据每日汇报',
          markdown_output_dir: 'reports/daily',
        },
        etf_pool: {
          items: [
            {
              windcode: '510300.SH',
              display_name: '沪深300ETF',
              bucket: 'broad',
              index_or_theme: '沪深300',
              is_active: true,
              start_date: '2026-05-11',
              notes: '核心观察',
            },
          ],
        },
        thresholds: {
          scale_surge_pct: 10,
          scale_surge_amount_cny: 5,
          scale_drop_pct: -10,
          scale_drop_amount_cny: -5,
          consecutive_flow_days: 5,
        },
        model: {
          mode: 'rules_only',
        },
      },
    })
  }

  if (url.endsWith('/api/v1/config/audit')) {
    return mockJson({
      items: [
        {
          action: 'update',
          config_type: 'runtime',
          config_name: 'scheduler.cron_daily',
          actor: 'admin:admin',
          actor_ip: '127.0.0.1',
          timestamp: '2026-05-11T12:00:00Z',
          details: { result: 'ok', masked: '****7890' },
        },
      ],
    })
  }

  if (url.endsWith('/api/v1/config/secrets/wind_api_key') && method === 'PUT') {
    return mockJson({
      name: 'wind_api_key',
      configured: true,
      masked: '****4321',
      ignored_secret: apiSecret,
    })
  }

  if (url.endsWith('/api/v1/config/secrets/wind_api_key') && method === 'DELETE') {
    return mockJson({ name: 'wind_api_key', deleted: true })
  }

  if (url.endsWith('/api/v1/config/test/wind')) {
    return mockJson({ status: 'ok', latency_ms: 123, ignored_secret: apiSecret })
  }

  if (url.includes('/api/v1/config/sections/')) {
    const section = url.split('/').pop()
    return mockJson({ section, values: JSON.parse(String(init?.body)).values })
  }

  return mockJson({ detail: 'not found' }, 404)
}

function installFetchMock() {
  const fetchMock = vi.fn(handleConfigFetch)
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('config page', () => {
  beforeEach(() => {
    installFetchMock()
    vi.spyOn(Storage.prototype, 'setItem')
    vi.spyOn(console, 'log').mockImplementation(() => undefined)
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
  })

  afterEach(() => {
    expect(console.log).not.toHaveBeenCalled()
    expect(console.error).not.toHaveBeenCalled()
    vi.restoreAllMocks()
  })

  it('renders the six configuration groups and global masked-only status', async () => {
    render(<App />)

    expect(await screen.findByRole('heading', { name: '系统配置' })).toBeInTheDocument()
    for (const name of [
      'Wind 数据源',
      '调度与日报发布',
      '重点 ETF 池',
      '阈值与口径',
      '模型与结论生成',
      '系统安全与审计',
    ]) {
      expect(screen.getByRole('heading', { name })).toBeInTheDocument()
    }

    expect(screen.getByText('已配置 · ****7890')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('****7890')).not.toBeInTheDocument()
    expect(screen.queryByText(/ak_/)).not.toBeInTheDocument()
    expect(
      screen.getByText('INVALID/MISSING/NOT_APPLICABLE 不落 0'),
    ).toBeInTheDocument()
    expect(screen.getByDisplayValue('10')).toBeInTheDocument()
    expect(screen.getByDisplayValue('-10')).toBeInTheDocument()
    expect(
      screen.getByDisplayValue(
        'INVALID/MISSING/NOT_APPLICABLE 不落 0；份额数据不可用时不打连续流入/流出标签',
      ),
    ).toBeInTheDocument()
  })

  it('clears newly entered secrets after save and never stores key material locally', async () => {
    render(<App />)
    await screen.findByText('已配置 · ****7890')

    const input = screen.getByLabelText('新 Wind API Key')
    fireEvent.change(input, { target: { value: 'ak_new_secret_1234564321' } })
    fireEvent.click(screen.getByRole('button', { name: '替换 Key' }))

    await waitFor(() => expect(input).toHaveValue(''))
    expect(screen.getByText('已替换')).toBeInTheDocument()
    expect(screen.getByText('已配置 · ****4321')).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('ak_new_secret_1234564321')
    expect(document.body).not.toHaveTextContent(apiSecret)
    expect(Storage.prototype.setItem).not.toHaveBeenCalled()
  })

  it('shows auth-specific copy for ETF snapshots and Wind key save before login', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (
        url.endsWith('/api/v1/config/status') ||
        url.endsWith('/api/v1/config/audit') ||
        url.endsWith('/api/v1/etf/snapshots')
      ) {
        return mockJson({ detail: 'Not authenticated' }, 401)
      }
      return handleConfigFetch(input)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    expect(await screen.findByRole('heading', { name: '登录' })).toBeInTheDocument()
    expect(screen.getByText('请先登录后查看 ETF 快照。')).toBeInTheDocument()

    const input = screen.getByLabelText('新 Wind API Key')
    fireEvent.change(input, { target: { value: 'ak_new_secret_1234564321' } })
    fireEvent.click(screen.getByRole('button', { name: '替换 Key' }))

    expect(screen.getByText('请先登录后保存 Wind Key')).toBeInTheDocument()
    expect(window.confirm).not.toHaveBeenCalled()
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/config/secrets/wind_api_key'),
      expect.objectContaining({ method: 'PUT' }),
    )
  })

  it('shows delete failure feedback without exposing key material', async () => {
    const fetchMock = installFetchMock()
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url.endsWith('/api/v1/config/secrets/wind_api_key') && method === 'DELETE') {
        return mockJson({ detail: 'delete failed', ignored_secret: apiSecret }, 500)
      }
      return handleConfigFetch(input, init)
    })

    render(<App />)
    await screen.findByText('已配置 · ****7890')
    fireEvent.click(screen.getByRole('button', { name: '删除 Key' }))

    expect(await screen.findByText('删除失败，请重试')).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(apiSecret)
  })

  it('renders Wind connection test result without secret fields', async () => {
    render(<App />)
    await screen.findByText('已配置 · ****7890')

    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))

    await waitFor(() => expect(screen.getAllByText('测试通过').length).toBeGreaterThan(0))
    expect(screen.getByText('123 ms')).toBeInTheDocument()
    expect(screen.getByText('Wind CLI')).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('wind_api_key')
    expect(document.body).not.toHaveTextContent(apiSecret)
  })

  it('shows operational Wind test failures without blaming the saved key', async () => {
    const fetchMock = installFetchMock()
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v1/config/test/wind')) {
        return mockJson({ detail: 'Wind connection test failed: backend unavailable' }, 502)
      }
      return handleConfigFetch(input, init)
    })

    render(<App />)
    await screen.findByText('已配置 · ****7890')

    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))

    expect(await screen.findByText('Wind 连接测试失败，请稍后重试')).toBeInTheDocument()
    expect(screen.queryByText('检查 key 或点击替换')).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(apiSecret)
  })

  it('surfaces audit rows without rendering secret details', async () => {
    render(<App />)

    const audit = await screen.findByTestId('audit-list')
    expect(within(audit).getByText('scheduler.cron_daily')).toBeInTheDocument()
    expect(within(audit).getByText('admin:admin')).toBeInTheDocument()
    expect(within(audit).queryByText('****7890')).not.toBeInTheDocument()
  })
})
