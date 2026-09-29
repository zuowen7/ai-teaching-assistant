import { computed, ref } from 'vue'
import { API_BASE } from '../utils/api'
import { i18n } from '../i18n'
import { useFileTree } from './useFileTree'

export type LiteratureAnswerStatus = 'answered' | 'insufficient'
export type LiteratureInsufficientReason =
  | 'no_retrieval_hits'
  | 'no_resolvable_evidence'
  | 'model_reported_insufficient'
  | 'all_claims_rejected'
export type LiteratureEvidenceStatus = 'supported' | 'conflicting' | 'insufficient'
export type LiteratureRejectedReason =
  | 'unknown_evidence_id'
  | 'fabricated_page_reference'
  | 'missing_evidence'
  | 'model_reported_insufficient'
  | 'empty_claim_text'
  | 'claim_limit_exceeded'

export interface LiteratureEvidenceSpan {
  evidence_id: string
  source_id: string
  artifact_sha256: string
  page_start: number
  page_end: number
  char_start: number
  char_end: number
  coordinate_space: string
  chunk_id: string
  exact_quote: string
  quote_sha256: string
  context_before: string
  context_after: string
  parser_version: string
  chunker_version: string
  embedding_model: string
  embedding_version: string
  index_version: string
}

export interface LiteratureClaim {
  claim_id: string
  text: string
  evidence_ids: string[]
  evidence_status: LiteratureEvidenceStatus
  model_provider: string
  model_name: string
  model_config_hash: string
  generated_at: string
}

export interface LiteratureEvidenceItem {
  source_id: string
  doc_id: string
  title: string
  chunk_id: string
  paper_id: string | null
  span: LiteratureEvidenceSpan
}

export interface LiteratureRejectedClaim {
  text: string
  reason: LiteratureRejectedReason
  detail: string
  evidence_ids: string[]
}

export interface LiteratureUnresolvedEvidence {
  source_id: string
  chunk_id: string | null
  reason: string
  detail: string
}

export interface LiteratureAnswerResult {
  question: string
  project_root: string
  source_ids: string[]
  status: LiteratureAnswerStatus
  insufficient_reason: LiteratureInsufficientReason | null
  claims: LiteratureClaim[]
  evidence: LiteratureEvidenceItem[]
  rejected_claims: LiteratureRejectedClaim[]
  unresolved: LiteratureUnresolvedEvidence[]
  retrieved_chunk_count: number
  model_provider: string
  model_name: string
  model_config_hash: string
  generated_at: string
}

export interface LiteratureFullTextResult {
  source_id: string
  status: string
  reused: boolean
  local_path: string | null
  source_url: string | null
  sha256: string | null
  file_size_bytes: number | null
  mime_type: string | null
  acquired_at: string | null
  failure_reason: string | null
}

export const DEFAULT_ANSWER_TOP_K = 5

const selectedSourceIds = ref<string[]>([])
const question = ref('')
const answer = ref<LiteratureAnswerResult | null>(null)
const answering = ref(false)
const error = ref('')
const expandedEvidenceIds = ref<string[]>([])
const acquiringSourceId = ref('')

function t(key: string, named?: Record<string, unknown>): string {
  return named ? i18n.global.t(key, named) : i18n.global.t(key)
}

function projectPath(): string {
  const root = useFileTree().rootDir.value
  if (!root) throw new Error(t('sources.projectRequired'))
  return root
}

async function parseError(response: Response): Promise<string> {
  const payload = (await response.json().catch(() => ({}))) as {
    detail?: string | { message?: string; code?: string } | Array<{ msg?: string }>
  }
  if (typeof payload.detail === 'string') return payload.detail
  if (Array.isArray(payload.detail)) {
    const first = payload.detail.find((item) => typeof item?.msg === 'string')
    if (first?.msg) return first.msg
  } else if (payload.detail && typeof payload.detail.message === 'string') {
    return payload.detail.message
  }
  return t('sources.requestFailed', { status: response.status })
}

/** Drop the previous answer; it belonged to a different question or scope. */
function resetAnswer(): void {
  answer.value = null
  error.value = ''
  expandedEvidenceIds.value = []
}

function setSelection(sourceIds: string[]): void {
  const normalized: string[] = []
  for (const raw of sourceIds) {
    const value = String(raw).trim()
    if (value && !normalized.includes(value)) normalized.push(value)
  }
  selectedSourceIds.value = normalized
  resetAnswer()
}

function toggleSource(sourceId: string, selected: boolean): void {
  const next = selected
    ? [...selectedSourceIds.value, sourceId]
    : selectedSourceIds.value.filter((item) => item !== sourceId)
  setSelection(next)
}

function clearSelection(): void {
  setSelection([])
}

function reset(): void {
  selectedSourceIds.value = []
  question.value = ''
  acquiringSourceId.value = ''
  resetAnswer()
}

function isSourceSelected(sourceId: string): boolean {
  return selectedSourceIds.value.includes(sourceId)
}

function toggleEvidence(evidenceId: string): void {
  expandedEvidenceIds.value = expandedEvidenceIds.value.includes(evidenceId)
    ? expandedEvidenceIds.value.filter((item) => item !== evidenceId)
    : [...expandedEvidenceIds.value, evidenceId]
}

function isEvidenceExpanded(evidenceId: string): boolean {
  return expandedEvidenceIds.value.includes(evidenceId)
}

/** The evidence a claim actually cites, in citation order. */
function evidenceForClaim(claim: LiteratureClaim): LiteratureEvidenceItem[] {
  const available = new Map(
    (answer.value?.evidence ?? []).map((item) => [item.span.evidence_id, item]),
  )
  return claim.evidence_ids
    .map((evidenceId) => available.get(evidenceId))
    .filter((item): item is LiteratureEvidenceItem => Boolean(item))
}

async function askQuestion(topK: number = DEFAULT_ANSWER_TOP_K): Promise<LiteratureAnswerResult> {
  const sourceIds = [...selectedSourceIds.value]
  if (!sourceIds.length) {
    const message = t('sources.answerScopeRequired')
    error.value = message
    throw new Error(message)
  }
  const asked = question.value.trim()
  if (!asked) {
    const message = t('sources.answerQuestionRequired')
    error.value = message
    throw new Error(message)
  }
  answering.value = true
  resetAnswer()
  try {
    const project = projectPath()
    const response = await fetch(`${API_BASE}/api/literature/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project_path: project,
        question: asked,
        source_ids: sourceIds,
        top_k: topK,
      }),
    })
    if (!response.ok) throw new Error(await parseError(response))
    const payload = (await response.json()) as LiteratureAnswerResult
    if (payload?.status !== 'answered' && payload?.status !== 'insufficient') {
      throw new Error(t('sources.answerResultInvalid'))
    }
    answer.value = payload
    return payload
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : t('sources.answerFailed')
    throw cause
  } finally {
    answering.value = false
  }
}

async function acquireFullText(sourceId: string, force = false): Promise<LiteratureFullTextResult> {
  const project = projectPath()
  acquiringSourceId.value = sourceId
  error.value = ''
  try {
    const response = await fetch(`${API_BASE}/api/literature/fulltext`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_path: project, source_id: sourceId, force }),
    })
    if (!response.ok) throw new Error(await parseError(response))
    return (await response.json()) as LiteratureFullTextResult
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : t('sources.fullTextAcquireFailed')
    throw cause
  } finally {
    acquiringSourceId.value = ''
  }
}

export function useLiteratureAnswer() {
  return {
    selectedSourceIds,
    question,
    answer,
    answering,
    error,
    expandedEvidenceIds,
    acquiringSourceId,
    // Claims and evidence are only exposed for an answered result: an
    // insufficient answer must never reach the UI as if it carried conclusions,
    // whatever the server (or a stale cache) sends.
    claims: computed(() => (answer.value?.status === 'answered' ? answer.value.claims : [])),
    evidence: computed(() => (answer.value?.status === 'answered' ? answer.value.evidence : [])),
    rejectedClaims: computed(() => answer.value?.rejected_claims ?? []),
    unresolved: computed(() => answer.value?.unresolved ?? []),
    status: computed(() => answer.value?.status ?? null),
    insufficientReason: computed(() => answer.value?.insufficient_reason ?? null),
    selectedCount: computed(() => selectedSourceIds.value.length),
    setSelection,
    toggleSource,
    selectAll: setSelection,
    clearSelection,
    isSourceSelected,
    toggleEvidence,
    isEvidenceExpanded,
    evidenceForClaim,
    askQuestion,
    acquireFullText,
    reset,
  }
}

export function _resetLiteratureAnswerForTesting(): void {
  reset()
  answering.value = false
}
