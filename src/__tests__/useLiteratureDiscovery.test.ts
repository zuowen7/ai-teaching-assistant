import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useFileTree } from '../composables/useFileTree'
import {
  _resetLiteratureDiscoveryForTesting,
  suggestArxivQuery,
  useLiteratureDiscovery,
} from '../composables/useLiteratureDiscovery'
import { _resetSourceLibraryForTesting } from '../composables/useSourceLibrary'

vi.mock('../utils/api', () => ({ API_BASE: 'http://127.0.0.1:18088' }))
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }))
vi.mock('@tauri-apps/api/event', () => ({ listen: vi.fn() }))

const record = {
  paper_id: 'paper_123',
  provider: 'arxiv',
  provider_record_id: '2501.00001',
  external_ids: { doi: null, arxiv: '2501.00001', arxiv_version: 2, other: {} },
  title: 'Evidence-Grounded Research Assistance',
  authors: ['Ada Researcher'],
  year: 2025,
  venue: null,
  abstract: 'A structured result.',
  categories: ['cs.AI'],
  record_url: 'https://arxiv.org/abs/2501.00001v2',
  access_locations: [
    {
      kind: 'pdf',
      url: 'https://arxiv.org/pdf/2501.00001v2',
      access_status: 'open',
      mime_type: 'application/pdf',
      license: null,
      is_primary: true,
    },
  ],
  source_query: 'all:"multi agent writing"',
  retrieved_at: '2026-09-21T00:00:00Z',
  normalization_version: 'literature-v1',
  metadata_snapshot_hash: 'a'.repeat(64),
}

const searchPage = {
  provider: 'arxiv',
  result_mode: 'live',
  query: {
    query: 'all:"multi agent writing"',
    page: 1,
    page_size: 10,
    sort_by: 'relevance',
    sort_order: 'descending',
    filters: { year_from: null, year_to: null, categories: [] },
  },
  records: [record],
  total_results: 1,
  has_more: false,
  retrieved_at: '2026-09-21T00:00:00Z',
  result_snapshot_id: `search_${'b'.repeat(24)}`,
  provenance_label: null,
}

const searchPlan = {
  researchQuestion: 'How do multi-agent systems support academic writing?',
  suggestedQuery: 'all:"multi agent academic writing"',
  generationMethod: 'template' as const,
  generationModel: null,
  generationConfig: { template: 'arxiv_all_phrase_v1' },
}

const searchExecutionId = `search_exec_${'c'.repeat(32)}`
const searchExecution = {
  search_execution_id: searchExecutionId,
  page: searchPage,
  plan: {
    research_question: searchPlan.researchQuestion,
    suggested_query: searchPlan.suggestedQuery,
    executed_query: searchPage.query,
    provider: 'arxiv',
    generation_method: 'template',
    generation_model: null,
    generation_config: searchPlan.generationConfig,
    confirmed_at: '2026-09-21T00:00:00Z',
    executed_at: '2026-09-21T00:00:01Z',
    result_snapshot_id: searchPage.result_snapshot_id,
  },
}

describe('useLiteratureDiscovery', () => {
  beforeEach(() => {
    _resetLiteratureDiscoveryForTesting()
    _resetSourceLibraryForTesting()
    useFileTree().rootDir.value = 'D:/papers/project-a'
    vi.restoreAllMocks()
  })

  it('builds a transparent editable arXiv phrase query', () => {
    expect(suggestArxivQuery('  Multi-Agent "academic" writing  ')).toBe(
      'all:"Multi-Agent academic writing"',
    )
  })

  it('submits the confirmed provider expression without reinterpretation', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify(searchExecution), { status: 200 }))

    const discovery = useLiteratureDiscovery()
    await discovery.searchLiterature(' arxiv ', '  all:"multi agent writing"  ', searchPlan)

    const [, init] = fetchMock.mock.calls[0]
    const body = JSON.parse(String(init?.body))
    expect(body).toMatchObject({
      provider: 'arxiv',
      plan: {
        research_question: searchPlan.researchQuestion,
        suggested_query: searchPlan.suggestedQuery,
        generation_method: 'template',
        generation_model: null,
        generation_config: { template: 'arxiv_all_phrase_v1' },
      },
      query: { query: 'all:"multi agent writing"', page: 1, page_size: 10 },
    })
    expect(discovery.page.value?.result_snapshot_id).toBe(searchPage.result_snapshot_id)
    expect(discovery.searchExecutionId.value).toBe(searchExecutionId)
  })

  it('imports only server-authorized snapshot paper ids and clears the selection', async () => {
    const source = {
      id: 'src_lit_1',
      title: record.title,
      original_path: null,
      translated_path: null,
      translation_task_id: null,
      rag_status: 'unavailable',
      reading_status: 'unread',
      cited: false,
      metadata: { literature: {} },
      created_at: '2026-09-21T00:00:00Z',
      updated_at: '2026-09-21T00:00:00Z',
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
      const target = String(url)
      if (target.endsWith('/api/literature/search')) {
        return new Response(JSON.stringify(searchExecution), { status: 200 })
      }
      if (target.endsWith('/api/literature/import')) {
        return new Response(
          JSON.stringify({
            search_execution_id: searchExecutionId,
            result_snapshot_id: searchPage.result_snapshot_id,
            results: [
              {
                paper_id: record.paper_id,
                source_id: source.id,
                disposition: 'created',
                matched_by: null,
                possible_duplicate_source_ids: [],
                source,
              },
            ],
            created_count: 1,
            reused_count: 0,
            metadata_updated_count: 0,
          }),
          { status: 200 },
        )
      }
      if (target.includes('/api/project/sources?')) {
        return new Response(JSON.stringify({ sources: [source] }), { status: 200 })
      }
      throw new Error(`unexpected request: ${target} ${init?.method ?? 'GET'}`)
    })
    const discovery = useLiteratureDiscovery()
    await discovery.searchLiterature('arxiv', searchPage.query.query, searchPlan)
    discovery.setPaperSelected(record.paper_id, true)

    const batch = await discovery.importSelected()

    const importCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith('/api/literature/import'),
    )
    expect(JSON.parse(String(importCall?.[1]?.body))).toEqual({
      project_path: 'D:/papers/project-a',
      search_execution_id: searchExecutionId,
      paper_ids: [record.paper_id],
    })
    expect(batch.created_count).toBe(1)
    expect(discovery.selectedPaperIds.value).toEqual([])
    expect(discovery.error.value).toBe('')
  })

  it('keeps the selection and reports the server message when import fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const target = String(url)
      if (target.endsWith('/api/literature/search')) {
        return new Response(JSON.stringify(searchExecution), { status: 200 })
      }
      return new Response(
        JSON.stringify({
          detail: { code: 'record_not_in_snapshot', message: '所选论文不属于该检索快照' },
        }),
        { status: 400 },
      )
    })
    const discovery = useLiteratureDiscovery()
    await discovery.searchLiterature('arxiv', searchPage.query.query, searchPlan)
    discovery.setPaperSelected(record.paper_id, true)

    await expect(discovery.importSelected()).rejects.toThrow('所选论文不属于该检索快照')
    expect(discovery.selectedPaperIds.value).toEqual([record.paper_id])
    expect(discovery.error.value).toBe('所选论文不属于该检索快照')
    expect(discovery.importing.value).toBe(false)
  })

  it('resetDiscoveryState drops the snapshot, execution handle and selection', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(searchExecution), { status: 200 }),
    )
    const discovery = useLiteratureDiscovery()
    await discovery.searchLiterature('arxiv', searchPage.query.query, searchPlan)
    discovery.setPaperSelected(record.paper_id, true)
    discovery.error.value = 'stale error'

    discovery.resetDiscoveryState()

    expect(discovery.page.value).toBeNull()
    expect(discovery.searchExecutionId.value).toBe('')
    expect(discovery.selectedPaperIds.value).toEqual([])
    expect(discovery.error.value).toBe('')
  })

  it('surfaces structured provider failures instead of presenting an empty result', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ detail: { code: 'unavailable', message: '文献源当前不可用' } }),
        { status: 503 },
      ),
    )
    const discovery = useLiteratureDiscovery()

    await expect(discovery.searchLiterature('arxiv', 'all:test', searchPlan)).rejects.toThrow(
      '文献源当前不可用',
    )
    expect(discovery.page.value).toBeNull()
    expect(discovery.error.value).toBe('文献源当前不可用')
  })
})
