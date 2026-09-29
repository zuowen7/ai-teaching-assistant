import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SourceLibraryView from '../components/SourceLibraryView.vue'
import { useFileTree } from '../composables/useFileTree'
import {
  _resetLiteratureAnswerForTesting,
  useLiteratureAnswer,
} from '../composables/useLiteratureAnswer'
import { _resetSourceLibraryForTesting } from '../composables/useSourceLibrary'

// The real composer keeps locale keys honest: a missing key renders as the raw
// key and fails the assertions below.
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

const PROJECT = 'D:/papers/project-a'
const EVIDENCE_ID = `evidence_${'a'.repeat(24)}`

const literatureSource = {
  id: 'src_lit_1',
  title: 'Evidence-Grounded Research Assistance',
  original_path: null,
  translated_path: null,
  translation_task_id: null,
  rag_status: 'ready',
  reading_status: 'unread',
  cited: false,
  metadata: { literature: { schema_version: 1 } },
  created_at: '2026-09-21T00:00:00Z',
  updated_at: '2026-09-21T00:00:00Z',
}

const plainSource = {
  ...literatureSource,
  id: 'src_plain_1',
  title: 'Hand written notes',
  metadata: {},
}

// Entered through the literature flow but never indexed: it cannot contribute
// evidence, so its checkbox must be disabled.
const pendingSource = {
  ...literatureSource,
  id: 'src_lit_pending',
  title: 'Demo Paper C',
  rag_status: 'unavailable',
  metadata: { literature: { schema_version: 1, fulltext: { status: 'metadata_only' } } },
}

const answered = {
  question: 'Which evaluation protocol was used?',
  project_root: PROJECT,
  source_ids: ['src_lit_1'],
  status: 'answered',
  insufficient_reason: null,
  claims: [
    {
      claim_id: `claim_${'d'.repeat(24)}`,
      text: '评估使用了留出集。',
      evidence_ids: [EVIDENCE_ID],
      evidence_status: 'supported',
      model_provider: 'openai',
      model_name: 'gpt-4o',
      model_config_hash: 'e'.repeat(64),
      generated_at: '2026-09-29T00:00:00Z',
    },
  ],
  evidence: [
    {
      source_id: 'src_lit_1',
      doc_id: 'project:src_lit_1',
      title: 'Evidence-Grounded Research Assistance',
      chunk_id: 'chunk_alpha',
      paper_id: 'paper_alpha',
      span: {
        evidence_id: EVIDENCE_ID,
        source_id: 'src_lit_1',
        artifact_sha256: 'b'.repeat(64),
        page_start: 2,
        page_end: 2,
        char_start: 10,
        char_end: 42,
        coordinate_space: 'normalized_page_text_v1',
        chunk_id: 'chunk_alpha',
        exact_quote: 'Only this page mentions the held-out split.',
        quote_sha256: 'c'.repeat(64),
        context_before: 'context before the quote',
        context_after: 'context after the quote',
        parser_version: 'parser-v1',
        chunker_version: 'chunker-v1',
        embedding_model: 'all-MiniLM-L6-v2',
        embedding_version: 'unpinned',
        index_version: 'index-v1',
      },
    },
  ],
  rejected_claims: [
    {
      text: 'As shown on page 9.',
      reason: 'fabricated_page_reference',
      detail: 'page references outside the cited evidence: 9',
      evidence_ids: [EVIDENCE_ID],
    },
  ],
  unresolved: [
    {
      source_id: 'src_lit_1',
      chunk_id: 'flat-chunk-1',
      reason: 'missing_page_metadata',
      detail: '展平文本没有页码',
    },
  ],
  retrieved_chunk_count: 2,
  model_provider: 'openai',
  model_name: 'gpt-4o',
  model_config_hash: 'e'.repeat(64),
  generated_at: '2026-09-29T00:00:00Z',
}

const insufficient = {
  ...answered,
  status: 'insufficient',
  insufficient_reason: 'no_resolvable_evidence',
  // Deliberately violating payload: a claim and its quote are present even
  // though the answer is insufficient; the UI must not render either.
  rejected_claims: [],
  unresolved: [],
  retrieved_chunk_count: 1,
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

interface RecordedCall {
  url: string
  method: string
}

function mockApi(answerPayload: unknown) {
  const calls: RecordedCall[] = []
  const fetchMock = vi
    .spyOn(globalThis, 'fetch')
    .mockImplementation((input, init?: RequestInit) => {
      const target = String(input)
      calls.push({ url: target, method: String(init?.method || 'GET').toUpperCase() })
      if (target.includes('/api/project/sources')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              sources: [literatureSource, pendingSource, plainSource],
            }),
            { status: 200 },
          ),
        )
      }
      if (target.endsWith('/api/literature/answer')) {
        return Promise.resolve(new Response(JSON.stringify(answerPayload), { status: 200 }))
      }
      if (target.endsWith('/api/literature/fulltext')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              source_id: 'src_lit_1',
              status: 'fulltext_ready',
              reused: false,
              local_path: 'D:/papers/project-a/references/alpha.pdf',
              source_url: 'https://arxiv.org/pdf/2501.00001v2',
              sha256: 'f'.repeat(64),
              file_size_bytes: 2048,
              mime_type: 'application/pdf',
              acquired_at: '2026-09-29T00:00:00Z',
              failure_reason: null,
            }),
            { status: 200 },
          ),
        )
      }
      throw new Error(`unexpected request: ${target}`)
    })
  return {
    fetchMock,
    calls,
    sourcesCalls: () => calls.filter((call) => call.url.includes('/api/project/sources')).length,
  }
}

async function openAnswerDialog(wrapper: ReturnType<typeof mountView>) {
  await wrapper.get('[data-testid="open-evidence-answer"]').trigger('click')
  await flushPromises()
}

async function selectLiteratureSource(wrapper: ReturnType<typeof mountView>) {
  await wrapper.get('[data-testid="answer-source-src_lit_1"]').setValue(true)
  await flushPromises()
}

describe('SourceLibraryView evidence answers', () => {
  beforeEach(() => {
    _resetLiteratureAnswerForTesting()
    _resetSourceLibraryForTesting()
    useFileTree().rootDir.value = PROJECT
    vi.restoreAllMocks()
  })

  it('answers inside the selected scope and renders a verifiable citation', async () => {
    const api = mockApi(answered)
    const wrapper = mountView()
    await flushPromises()

    await openAnswerDialog(wrapper)
    // Only sources that entered through the literature flow can answer.
    expect(wrapper.find('[data-testid="answer-source-src_lit_1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="answer-source-src_plain_1"]').exists()).toBe(false)

    await selectLiteratureSource(wrapper)
    await wrapper
      .get('[data-testid="answer-question"]')
      .setValue('Which evaluation protocol was used?')
    await wrapper.get('[data-testid="answer-form"]').trigger('submit')
    await flushPromises()

    const answerCall = api.fetchMock.mock.calls.find((call) =>
      String(call[0]).endsWith('/api/literature/answer'),
    )
    expect(answerCall).toBeDefined()
    expect(JSON.parse(String(answerCall?.[1]?.body))).toEqual({
      project_path: PROJECT,
      question: 'Which evaluation protocol was used?',
      source_ids: ['src_lit_1'],
      top_k: 5,
    })

    const result = wrapper.get('[data-testid="answer-result"]')
    expect(result.text()).toContain('评估使用了留出集。')
    expect(result.text()).toContain('第 2 页')
    expect(result.text()).toContain('Evidence-Grounded Research Assistance')
    expect(result.text()).toContain('1 条结论未通过证据校验')
    expect(result.text()).toContain('1 条检索片段无法核验')
    expect(result.text()).toContain('结论中的页码不在所引证据中')

    // The quote itself is not rendered until the citation is expanded.
    expect(wrapper.find(`[data-testid="evidence-quote-${EVIDENCE_ID}"]`).exists()).toBe(false)
    await wrapper.get(`[data-testid="evidence-toggle-${EVIDENCE_ID}"]`).trigger('click')
    const quote = wrapper.get(`[data-testid="evidence-quote-${EVIDENCE_ID}"]`)
    expect(quote.text()).toContain('Only this page mentions the held-out split.')
    expect(quote.text()).toContain('context before the quote')
    expect(quote.text()).toContain('context after the quote')
  })

  it('reports an explicit insufficiency instead of rendering claims', async () => {
    mockApi(insufficient)
    const wrapper = mountView()
    await flushPromises()

    await openAnswerDialog(wrapper)
    await selectLiteratureSource(wrapper)
    await wrapper.get('[data-testid="answer-question"]').setValue('Any question')
    await wrapper.get('[data-testid="answer-form"]').trigger('submit')
    await flushPromises()

    const result = wrapper.get('[data-testid="answer-result"]')
    expect(result.text()).toContain('证据不足')
    expect(result.text()).toContain('检索到的片段无法核验回页码与原文')
    // The payload carries a claim and a quote; neither may reach the UI.
    expect(wrapper.find('[data-testid="answer-claims"]').exists()).toBe(false)
    expect(result.text()).not.toContain('评估使用了留出集。')
    expect(result.text()).not.toContain('Only this page mentions the held-out split.')
  })

  it('disables literature sources that have no page-level index', async () => {
    mockApi(answered)
    const wrapper = mountView()
    await flushPromises()

    await openAnswerDialog(wrapper)

    // Both literature sources are listed, but only the indexed one can be picked.
    expect(wrapper.find('[data-testid="answer-source-src_lit_1"]').exists()).toBe(true)
    const pending = wrapper.get('[data-testid="answer-source-src_lit_pending"]')
    expect(pending.attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="answer-select-all"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="answer-scope"]').text()).toContain('已选 1 篇')
  })

  it('drops the answer and the scope when the project changes', async () => {
    mockApi(answered)
    const wrapper = mountView()
    await flushPromises()

    await openAnswerDialog(wrapper)
    await selectLiteratureSource(wrapper)
    await wrapper.get('[data-testid="answer-question"]').setValue('Which protocol?')
    await wrapper.get('[data-testid="answer-form"]').trigger('submit')
    await flushPromises()
    expect(wrapper.find('[data-testid="answer-result"]').exists()).toBe(true)

    useFileTree().rootDir.value = 'D:/papers/project-b'
    await flushPromises()

    expect(wrapper.find('[data-testid="evidence-answer-dialog"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="answer-result"]').exists()).toBe(false)
    expect(useLiteratureAnswer().selectedSourceIds.value).toEqual([])
    expect(useLiteratureAnswer().question.value).toBe('')
  })

  it('selects every answerable source and clears the scope', async () => {
    mockApi(answered)
    const wrapper = mountView()
    await flushPromises()

    await openAnswerDialog(wrapper)
    await wrapper.get('[data-testid="answer-select-all"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="answer-scope"]').text()).toContain('已选 1 篇')

    await wrapper.get('[data-testid="answer-clear-selection"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="answer-scope"]').text()).toContain('已选 0 篇')
  })

  it('fetches the declared open full text and re-reads the library', async () => {
    const api = mockApi(answered)
    const wrapper = mountView()
    await flushPromises()
    const callsBefore = api.sourcesCalls()

    await wrapper.get('[data-testid="acquire-open-fulltext"]').trigger('click')
    await flushPromises()

    const acquireCall = api.fetchMock.mock.calls.find((call) =>
      String(call[0]).endsWith('/api/literature/fulltext'),
    )
    expect(acquireCall).toBeDefined()
    expect(JSON.parse(String(acquireCall?.[1]?.body))).toEqual({
      project_path: PROJECT,
      source_id: 'src_lit_1',
      force: false,
    })
    // The server owns metadata.literature, so the client must re-read it and
    // never write a source back itself (D-022).
    expect(api.sourcesCalls()).toBeGreaterThan(callsBefore)
    const sourceWrites = api.calls.filter(
      (call) => call.url.includes('/api/project/sources') && call.method !== 'GET',
    )
    expect(sourceWrites).toEqual([])
  })
})
