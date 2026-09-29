import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useFileTree } from '../composables/useFileTree'
import {
  _resetLiteratureAnswerForTesting,
  useLiteratureAnswer,
} from '../composables/useLiteratureAnswer'

vi.mock('../utils/api', () => ({ API_BASE: 'http://127.0.0.1:18088' }))
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }))
vi.mock('@tauri-apps/api/event', () => ({ listen: vi.fn() }))

const PROJECT = 'D:/papers/project-a'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const evidenceSpan = {
  evidence_id: `evidence_${'a'.repeat(24)}`,
  source_id: 'src_alpha',
  artifact_sha256: 'b'.repeat(64),
  page_start: 2,
  page_end: 2,
  char_start: 10,
  char_end: 42,
  coordinate_space: 'normalized_page_text_v1',
  chunk_id: 'chunk_alpha',
  exact_quote: 'Only this page mentions the held-out split.',
  quote_sha256: 'c'.repeat(64),
  context_before: '',
  context_after: '',
  parser_version: 'parser-v1',
  chunker_version: 'chunker-v1',
  embedding_model: 'all-MiniLM-L6-v2',
  embedding_version: 'unpinned',
  index_version: 'index-v1',
}

const answeredResult = {
  question: 'What evaluation protocol was used?',
  project_root: PROJECT,
  source_ids: ['src_alpha'],
  status: 'answered',
  insufficient_reason: null,
  claims: [
    {
      claim_id: `claim_${'d'.repeat(24)}`,
      text: 'The study evaluates on a held-out split.',
      evidence_ids: [evidenceSpan.evidence_id],
      evidence_status: 'supported',
      model_provider: 'openai',
      model_name: 'gpt-4o',
      model_config_hash: 'e'.repeat(64),
      generated_at: '2026-09-29T00:00:00Z',
    },
  ],
  evidence: [
    {
      source_id: 'src_alpha',
      doc_id: 'project:src_alpha',
      title: 'Alpha Paper',
      chunk_id: 'chunk_alpha',
      paper_id: 'paper_alpha',
      span: evidenceSpan,
    },
  ],
  rejected_claims: [
    {
      text: 'As shown on page 9.',
      reason: 'fabricated_page_reference',
      detail: 'page references outside the cited evidence: 9',
      evidence_ids: [evidenceSpan.evidence_id],
    },
  ],
  unresolved: [
    {
      source_id: 'src_alpha',
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

const insufficientResult = {
  ...answeredResult,
  status: 'insufficient',
  insufficient_reason: 'no_retrieval_hits',
  // Deliberately violating payload: an insufficient answer that still carries
  // claims and evidence, so the "no conclusions" rule is actually exercised.
  retrieved_chunk_count: 0,
}

function answer(): ReturnType<typeof useLiteratureAnswer> {
  return useLiteratureAnswer()
}

describe('useLiteratureAnswer', () => {
  beforeEach(() => {
    _resetLiteratureAnswerForTesting()
    useFileTree().rootDir.value = PROJECT
    vi.restoreAllMocks()
  })

  it('refuses to ask without a source scope and never calls the backend', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')

    await expect(answer().askQuestion()).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(answer().error.value).toBeTruthy()
  })

  it('refuses a blank question and never calls the backend', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    answer().setSelection(['src_alpha'])

    await expect(answer().askQuestion()).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('posts project, question, scope and top_k to the answer route', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(answeredResult))
    const state = answer()
    state.setSelection(['src_alpha'])
    state.question.value = 'What evaluation protocol was used?'

    await state.askQuestion()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toBe('http://127.0.0.1:18088/api/literature/answer')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({
      project_path: PROJECT,
      question: 'What evaluation protocol was used?',
      source_ids: ['src_alpha'],
      top_k: 5,
    })
  })

  it('exposes claims together with only their own evidence', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(answeredResult))
    const state = answer()
    state.setSelection(['src_alpha'])
    state.question.value = 'What evaluation protocol was used?'

    const result = await state.askQuestion()

    expect(result.status).toBe('answered')
    expect(state.answer.value?.status).toBe('answered')
    expect(state.status.value).toBe('answered')
    expect(state.claims.value).toHaveLength(1)
    expect(state.evidenceForClaim(state.claims.value[0])).toEqual(answeredResult.evidence)
    expect(
      state.evidenceForClaim({ ...state.claims.value[0], evidence_ids: ['evidence_missing'] }),
    ).toEqual([])
  })

  it('keeps rejected claims and unresolved hits visible', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(answeredResult))
    const state = answer()
    state.setSelection(['src_alpha'])
    state.question.value = 'q'

    await state.askQuestion()

    expect(state.rejectedClaims.value[0].reason).toBe('fabricated_page_reference')
    expect(state.unresolved.value[0].reason).toBe('missing_page_metadata')
  })

  it('reports an explicit insufficiency without inventing claims', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(insufficientResult))
    const state = answer()
    state.setSelection(['src_alpha'])
    state.question.value = 'q'

    await state.askQuestion()

    expect(state.status.value).toBe('insufficient')
    expect(state.insufficientReason.value).toBe('no_retrieval_hits')
    expect(state.claims.value).toEqual([])
    expect(state.evidence.value).toEqual([])
  })

  it('surfaces the server failure message and clears the previous answer', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ detail: { code: 'scope_required', message: '必须选择文献' } }, 400),
    )
    const state = answer()
    state.setSelection(['src_alpha'])
    state.question.value = 'q'

    await expect(state.askQuestion()).rejects.toThrow('必须选择文献')

    expect(state.error.value).toBe('必须选择文献')
    expect(state.answer.value).toBeNull()
    expect(state.answering.value).toBe(false)
  })

  it('manages the scope selection', () => {
    const state = answer()

    state.setSelection(['src_alpha', 'src_beta'])
    expect(state.selectedSourceIds.value).toEqual(['src_alpha', 'src_beta'])
    expect(state.isSourceSelected('src_alpha')).toBe(true)

    state.toggleSource('src_alpha', false)
    expect(state.selectedSourceIds.value).toEqual(['src_beta'])

    state.toggleSource('src_alpha', true)
    expect(state.selectedSourceIds.value).toEqual(['src_beta', 'src_alpha'])

    state.selectAll(['src_a', 'src_b'])
    expect(state.selectedSourceIds.value).toEqual(['src_a', 'src_b'])

    state.selectAll([])
    expect(state.selectedSourceIds.value).toEqual([])
  })

  it('drops the previous answer when the scope changes', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      Promise.resolve(jsonResponse(answeredResult)),
    )
    const state = answer()
    state.setSelection(['src_alpha'])
    state.question.value = 'q'
    await state.askQuestion()
    expect(state.answer.value).not.toBeNull()

    state.toggleSource('src_beta', true)
    expect(state.answer.value).toBeNull()

    await state.askQuestion()
    expect(state.answer.value).not.toBeNull()
    state.clearSelection()
    expect(state.answer.value).toBeNull()
  })

  it('expands and collapses one evidence item at a time', () => {
    const state = answer()
    expect(state.isEvidenceExpanded('evidence_a')).toBe(false)

    state.toggleEvidence('evidence_a')
    expect(state.isEvidenceExpanded('evidence_a')).toBe(true)
    state.toggleEvidence('evidence_a')
    expect(state.isEvidenceExpanded('evidence_a')).toBe(false)
  })

  it('acquires an open full text through the literature route', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        source_id: 'src_alpha',
        status: 'fulltext_ready',
        reused: false,
        local_path: 'D:/papers/project-a/references/alpha.pdf',
        source_url: 'https://arxiv.org/pdf/2501.00001v2',
        sha256: 'f'.repeat(64),
        file_size_bytes: 1024,
        mime_type: 'application/pdf',
        acquired_at: '2026-09-29T00:00:00Z',
        failure_reason: null,
      }),
    )
    const state = answer()

    const result = await state.acquireFullText('src_alpha')

    expect(result.status).toBe('fulltext_ready')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toBe('http://127.0.0.1:18088/api/literature/fulltext')
    expect(JSON.parse(String(init?.body))).toEqual({
      project_path: PROJECT,
      source_id: 'src_alpha',
      force: false,
    })
    expect(state.acquiringSourceId.value).toBe('')
  })

  it('reports a refused open full text without throwing away its reason', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ detail: { code: 'access_unavailable', message: '没有开放全文' } }, 409),
    )
    const state = answer()

    await expect(state.acquireFullText('src_alpha')).rejects.toThrow('没有开放全文')

    expect(state.error.value).toBe('没有开放全文')
    expect(state.acquiringSourceId.value).toBe('')
  })

  it('resets every piece of state', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(answeredResult))
    const state = answer()
    state.setSelection(['src_alpha'])
    state.question.value = 'q'
    await state.askQuestion()
    state.toggleEvidence(evidenceSpan.evidence_id)

    state.reset()

    expect(state.selectedSourceIds.value).toEqual([])
    expect(state.question.value).toBe('')
    expect(state.answer.value).toBeNull()
    expect(state.error.value).toBe('')
    expect(state.expandedEvidenceIds.value).toEqual([])
  })
})
