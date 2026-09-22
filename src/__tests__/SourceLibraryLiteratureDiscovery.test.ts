import { flushPromises, mount } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SourceLibraryView from '../components/SourceLibraryView.vue'
import { useFileTree } from '../composables/useFileTree'
import { _resetLiteratureDiscoveryForTesting } from '../composables/useLiteratureDiscovery'
import { _resetSourceLibraryForTesting } from '../composables/useSourceLibrary'

// Keep the real createI18n so the composable's own translations resolve, but
// route component-level t() through the real locale files: a key that is
// missing from them renders as the raw key and fails a test.  The composer is
// built here (from the JSON locales) instead of importing ../i18n, because a
// mock factory that re-imports a module depending on the mocked one deadlocks.
vi.mock('vue-i18n', async (importOriginal) => {
  const actual = await importOriginal<typeof import('vue-i18n')>()
  const zhCN = (await import('../i18n/locales/zh-CN.json')).default
  const composer = actual.createI18n({
    legacy: false,
    locale: 'zh-CN',
    fallbackLocale: 'zh-CN',
    messages: { 'zh-CN': zhCN },
  })
  return {
    ...actual,
    useI18n: () => ({
      t: (key: string, named?: Record<string, unknown>) =>
        named ? composer.global.t(key, named) : composer.global.t(key),
    }),
  }
})
vi.mock('../utils/api', () => ({ API_BASE: 'http://127.0.0.1:18088' }))
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }))
vi.mock('@tauri-apps/api/event', () => ({ listen: vi.fn() }))
vi.mock('@tauri-apps/plugin-dialog', () => ({ open: vi.fn() }))
vi.mock('../composables/useToast', () => ({
  useToast: () => ({ pushError: vi.fn(), success: vi.fn() }),
}))
vi.mock('../composables/useTranslate', () => ({
  useTranslate: () => ({
    state: { status: 'idle', taskId: '', outputPath: '', errorMessage: '' },
    overallProgress: () => 0,
    translateFromPath: vi.fn(),
  }),
}))
vi.mock('../composables/useEditorCitation', () => ({
  useEditorCitation: () => ({
    getZoteroStatus: vi.fn(),
    searchZotero: vi.fn(),
  }),
}))

const paper = {
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

const page = {
  provider: 'arxiv',
  result_mode: 'live',
  query: {
    query: 'ti:"multi-agent" AND all:"academic writing"',
    page: 1,
    page_size: 10,
    sort_by: 'relevance',
    sort_order: 'descending',
    filters: { year_from: null, year_to: null, categories: [] },
  },
  records: [paper],
  total_results: 1,
  has_more: false,
  retrieved_at: '2026-09-21T00:00:00Z',
  result_snapshot_id: `search_${'b'.repeat(24)}`,
  provenance_label: null,
}

const searchExecutionId = `search_exec_${'c'.repeat(32)}`
const searchExecution = {
  search_execution_id: searchExecutionId,
  page,
  plan: {
    research_question: 'Multi-Agent academic writing',
    suggested_query: 'all:"Multi-Agent academic writing"',
    executed_query: page.query,
    provider: 'arxiv',
    generation_method: 'template',
    generation_model: null,
    generation_config: { template: 'arxiv_all_phrase_v1' },
    confirmed_at: '2026-09-21T00:00:00Z',
    executed_at: '2026-09-21T00:00:01Z',
    result_snapshot_id: page.result_snapshot_id,
  },
}

const source = {
  id: 'src_lit_1',
  title: paper.title,
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

const providersPayload = {
  providers: [
    {
      provider: 'arxiv',
      result_mode: 'live',
      supports_search: true,
      supports_record_lookup: true,
      supports_access_resolution: true,
      supports_fulltext_download: true,
      supports_pagination: true,
      max_page_size: 100,
    },
  ],
}

function mockLiteratureApi() {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
    const target = String(url)
    if (target.endsWith('/api/literature/providers')) {
      return new Response(JSON.stringify(providersPayload), { status: 200 })
    }
    if (target.endsWith('/api/literature/search')) {
      return new Response(JSON.stringify(searchExecution), { status: 200 })
    }
    if (target.endsWith('/api/literature/import')) {
      return new Response(
        JSON.stringify({
          search_execution_id: searchExecutionId,
          result_snapshot_id: page.result_snapshot_id,
          results: [
            {
              paper_id: paper.paper_id,
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
    throw new Error(`unexpected request: ${target}`)
  })
}

function mountView() {
  return mount(SourceLibraryView, {
    props: {
      healthOk: true,
      backendRestarting: false,
      readSettings: { fontSize: 16, lineHeight: 1.6, fontFamily: 'serif', transColor: '' },
    },
  })
}

async function runSearch(wrapper: ReturnType<typeof mountView>, question: string, query: string) {
  await wrapper.get('[data-testid="open-literature-discovery"]').trigger('click')
  await flushPromises()
  await wrapper.get('[data-testid="literature-research-question"]').setValue(question)
  const buildButton = wrapper
    .findAll('button')
    .find((button) => button.text().includes('生成检索式'))
  expect(buildButton).toBeDefined()
  await buildButton!.trigger('click')
  await wrapper.get('[data-testid="literature-confirmed-query"]').setValue(query)
  await wrapper.get('[data-testid="literature-search-form"]').trigger('submit')
  await flushPromises()
}

describe('SourceLibraryView literature discovery', () => {
  beforeEach(() => {
    _resetLiteratureDiscoveryForTesting()
    _resetSourceLibraryForTesting()
    useFileTree().rootDir.value = 'D:/papers/project-a'
    vi.restoreAllMocks()
  })

  it('shows the exact confirmed query and imports selected snapshot records', async () => {
    const fetchMock = mockLiteratureApi()
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="open-literature-discovery"]').trigger('click')
    await flushPromises()
    await wrapper
      .get('[data-testid="literature-research-question"]')
      .setValue('Multi-Agent academic writing')
    const buildButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('生成检索式'))
    expect(buildButton).toBeDefined()
    await buildButton!.trigger('click')
    expect(
      wrapper.get<HTMLInputElement>('[data-testid="literature-confirmed-query"]').element.value,
    ).toBe('all:"Multi-Agent academic writing"')
    await wrapper
      .get('[data-testid="literature-confirmed-query"]')
      .setValue('ti:"multi-agent" AND all:"academic writing"')

    await wrapper.get('[data-testid="literature-search-form"]').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[data-testid="literature-query-receipt"]').text()).toContain(
      'ti:"multi-agent" AND all:"academic writing"',
    )

    const searchCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith('/api/literature/search'),
    )
    expect(JSON.parse(String(searchCall?.[1]?.body))).toMatchObject({
      provider: 'arxiv',
      query: { query: 'ti:"multi-agent" AND all:"academic writing"' },
      plan: {
        research_question: 'Multi-Agent academic writing',
        suggested_query: 'all:"Multi-Agent academic writing"',
        generation_method: 'template',
        generation_model: null,
        generation_config: { template: 'arxiv_all_phrase_v1' },
      },
    })

    await wrapper.get(`[data-testid="literature-select-${paper.paper_id}"]`).setValue(true)
    await wrapper.get('[data-testid="literature-import-selected"]').trigger('click')
    await flushPromises()

    const importCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith('/api/literature/import'),
    )
    expect(JSON.parse(String(importCall?.[1]?.body))).toEqual({
      project_path: 'D:/papers/project-a',
      search_execution_id: searchExecutionId,
      paper_ids: [paper.paper_id],
    })
  })

  it('renders the live-mode receipt label from the locale files', async () => {
    mockLiteratureApi()
    const wrapper = mountView()
    await flushPromises()

    await runSearch(wrapper, 'Multi-Agent academic writing', 'all:"multi agent writing"')

    const receipt = wrapper.get('[data-testid="literature-query-receipt"]').text()
    expect(receipt).toContain('实时结果')
    expect(receipt).not.toContain('sources.resultMode')
  })

  it('keeps the receipt mode badge off surface-only colour tokens', () => {
    const source = readFileSync(
      resolve(process.cwd(), 'src/components/SourceLibraryView.vue'),
      'utf8',
    )
    const liveRule = /\.query-receipt \.mode-live \{[\s\S]*?\}/.exec(source)?.[0] ?? ''
    const cachedRule = /\.query-receipt \.mode-cache,[\s\S]*?\}/.exec(source)?.[0] ?? ''

    // The receipt is a paper-coloured panel, so --c-success/--c-warn measured
    // ~1.5:1 there in the dark token set.
    expect(liveRule).toContain('#1f5a34')
    expect(cachedRule).toContain('#7a4d0c')
    for (const rule of [liveRule, cachedRule]) {
      expect(rule).not.toContain('var(--c-success)')
      expect(rule).not.toContain('var(--c-warn)')
    }
  })

  it('drops the previous project snapshot when the open project changes', async () => {
    mockLiteratureApi()
    const wrapper = mountView()
    await flushPromises()

    await runSearch(wrapper, 'Multi-Agent academic writing', 'all:"multi agent writing"')
    expect(wrapper.find('[data-testid="literature-query-receipt"]').exists()).toBe(true)

    useFileTree().rootDir.value = 'D:/papers/project-b'
    await flushPromises()

    expect(wrapper.find('[data-testid="literature-query-receipt"]').exists()).toBe(false)
    expect(
      wrapper.get<HTMLInputElement>('[data-testid="literature-confirmed-query"]').element.value,
    ).toBe('')
    expect(
      wrapper.get<HTMLTextAreaElement>('[data-testid="literature-research-question"]').element
        .value,
    ).toBe('')
  })
})
