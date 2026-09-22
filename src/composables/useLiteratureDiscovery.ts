import { computed, ref } from 'vue'
import { API_BASE } from '../utils/api'
import { i18n } from '../i18n'
import { useFileTree } from './useFileTree'
import { type ProjectSource } from './useSourceLibrary'

export type LiteratureResultMode = 'live' | 'cache' | 'fixture'
export type LiteratureAccessStatus = 'open' | 'restricted' | 'unknown'
export type LiteratureImportDisposition = 'created' | 'reused' | 'metadata_updated'
export type LiteratureQueryGenerationMethod = 'user' | 'template' | 'llm'

export interface LiteratureSearchPlanDraft {
  researchQuestion: string
  suggestedQuery: string
  generationMethod: LiteratureQueryGenerationMethod
  generationModel?: string | null
  generationConfig?: Record<string, unknown>
}

export interface LiteratureProviderCapabilities {
  provider: string
  result_mode: LiteratureResultMode
  supports_search: boolean
  supports_record_lookup: boolean
  supports_access_resolution: boolean
  supports_fulltext_download: boolean
  supports_pagination: boolean
  max_page_size: number
}

export interface LiteratureAccessLocation {
  kind: 'pdf' | 'html' | 'landing_page' | 'repository'
  url: string
  access_status: LiteratureAccessStatus
  mime_type: string | null
  license: string | null
  is_primary: boolean
}

export interface LiteraturePaperRecord {
  paper_id: string
  provider: string
  provider_record_id: string
  external_ids: {
    doi: string | null
    arxiv: string | null
    arxiv_version: number | null
    other: Record<string, string>
  }
  title: string
  authors: string[]
  year: number | null
  venue: string | null
  abstract: string
  categories: string[]
  record_url: string
  access_locations: LiteratureAccessLocation[]
  source_query: string
  retrieved_at: string
  normalization_version: string
  metadata_snapshot_hash: string
}

export interface LiteratureSearchPage {
  provider: string
  result_mode: LiteratureResultMode
  query: {
    query: string
    page: number
    page_size: number
    sort_by: 'relevance' | 'year'
    sort_order: 'descending' | 'ascending'
    filters: {
      year_from: number | null
      year_to: number | null
      categories: string[]
    }
  }
  records: LiteraturePaperRecord[]
  total_results: number | null
  has_more: boolean
  retrieved_at: string
  result_snapshot_id: string
  provenance_label: string | null
}

export interface LiteratureSearchPlan {
  research_question: string
  suggested_query: string
  executed_query: LiteratureSearchPage['query']
  provider: string
  generation_method: LiteratureQueryGenerationMethod
  generation_model: string | null
  generation_config: Record<string, unknown>
  confirmed_at: string
  executed_at: string
  result_snapshot_id: string
}

export interface LiteratureSearchExecution {
  search_execution_id: string
  page: LiteratureSearchPage
  plan: LiteratureSearchPlan
}

export interface LiteratureImportItem {
  paper_id: string
  source_id: string
  disposition: LiteratureImportDisposition
  matched_by: 'doi' | 'arxiv' | 'paper_id' | null
  possible_duplicate_source_ids: string[]
  source: ProjectSource
}

export interface LiteratureImportBatch {
  search_execution_id: string
  result_snapshot_id: string
  results: LiteratureImportItem[]
  created_count: number
  reused_count: number
  metadata_updated_count: number
}

const providers = ref<LiteratureProviderCapabilities[]>([])
const page = ref<LiteratureSearchPage | null>(null)
const searchExecutionId = ref('')
const selectedPaperIds = ref<string[]>([])
const loadingProviders = ref(false)
const searching = ref(false)
const importing = ref(false)
const error = ref('')

function t(key: string, named?: Record<string, unknown>): string {
  return named ? i18n.global.t(key, named) : i18n.global.t(key)
}

/** Drop the current search snapshot, execution handle and selection. */
function resetDiscoveryState(): void {
  page.value = null
  searchExecutionId.value = ''
  selectedPaperIds.value = []
  error.value = ''
}

function projectPath(): string {
  const root = useFileTree().rootDir.value
  if (!root) throw new Error(t('sources.projectRequired'))
  return root
}

async function parseError(response: Response): Promise<string> {
  const payload = (await response.json().catch(() => ({}))) as {
    detail?: string | { message?: string } | Array<{ msg?: string }>
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

export function suggestArxivQuery(researchQuestion: string): string {
  const phrase = researchQuestion.replace(/["\\]/g, ' ').replace(/\s+/g, ' ').trim()
  return phrase ? `all:"${phrase}"` : ''
}

async function loadProviders(): Promise<LiteratureProviderCapabilities[]> {
  loadingProviders.value = true
  error.value = ''
  try {
    const response = await fetch(`${API_BASE}/api/literature/providers`)
    if (!response.ok) throw new Error(await parseError(response))
    const payload = (await response.json()) as {
      providers?: LiteratureProviderCapabilities[]
    }
    providers.value = Array.isArray(payload.providers) ? payload.providers : []
    return providers.value
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : t('sources.providerLoadFailed')
    throw cause
  } finally {
    loadingProviders.value = false
  }
}

async function searchLiterature(
  provider: string,
  confirmedQuery: string,
  plan: LiteratureSearchPlanDraft,
  options: { page?: number; pageSize?: number } = {},
): Promise<LiteratureSearchPage> {
  const normalizedProvider = provider.trim().toLocaleLowerCase()
  const normalizedQuery = confirmedQuery.replace(/\s+/g, ' ').trim()
  const normalizedResearchQuestion = plan.researchQuestion.replace(/\s+/g, ' ').trim()
  const normalizedSuggestedQuery = plan.suggestedQuery.replace(/\s+/g, ' ').trim()
  const normalizedGenerationModel = plan.generationModel?.trim() || null
  if (!normalizedProvider) throw new Error(t('sources.providerRequired'))
  if (!normalizedQuery) throw new Error(t('sources.confirmedQueryRequired'))
  if (!normalizedResearchQuestion) throw new Error(t('sources.researchQuestionRequired'))
  if (!normalizedSuggestedQuery) throw new Error(t('sources.suggestedQueryRequired'))
  if (plan.generationMethod === 'llm' && !normalizedGenerationModel) {
    throw new Error(t('sources.generationModelRequired'))
  }

  searching.value = true
  error.value = ''
  resetDiscoveryState()
  try {
    const response = await fetch(`${API_BASE}/api/literature/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider: normalizedProvider,
        plan: {
          research_question: normalizedResearchQuestion,
          suggested_query: normalizedSuggestedQuery,
          generation_method: plan.generationMethod,
          generation_model: normalizedGenerationModel,
          generation_config: plan.generationConfig ?? {},
        },
        query: {
          query: normalizedQuery,
          page: options.page ?? 1,
          page_size: options.pageSize ?? 10,
          sort_by: 'relevance',
          sort_order: 'descending',
          filters: { year_from: null, year_to: null, categories: [] },
        },
      }),
    })
    if (!response.ok) throw new Error(await parseError(response))
    const result = (await response.json()) as LiteratureSearchExecution
    if (!result.search_execution_id || !result.page) {
      throw new Error(t('sources.literatureResultInvalid'))
    }
    searchExecutionId.value = result.search_execution_id
    page.value = result.page
    return result.page
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : t('sources.literatureSearchFailed')
    throw cause
  } finally {
    searching.value = false
  }
}

function setPaperSelected(paperId: string, selected: boolean): void {
  const available = page.value?.records.some((record) => record.paper_id === paperId) ?? false
  if (!available) throw new Error(t('sources.selectionNotInSnapshot'))
  const next = new Set(selectedPaperIds.value)
  if (selected) next.add(paperId)
  else next.delete(paperId)
  selectedPaperIds.value = [...next]
}

function selectAllVisible(selected: boolean): void {
  selectedPaperIds.value = selected
    ? (page.value?.records.map((record) => record.paper_id) ?? [])
    : []
}

async function importSelected(): Promise<LiteratureImportBatch> {
  const snapshot = page.value
  const executionId = searchExecutionId.value
  if (!snapshot || !executionId) throw new Error(t('sources.searchRequired'))
  if (!selectedPaperIds.value.length) throw new Error(t('sources.selectionRequired'))

  importing.value = true
  error.value = ''
  try {
    const response = await fetch(`${API_BASE}/api/literature/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project_path: projectPath(),
        search_execution_id: executionId,
        paper_ids: [...selectedPaperIds.value],
      }),
    })
    if (!response.ok) throw new Error(await parseError(response))
    const batch = (await response.json()) as LiteratureImportBatch
    // The import already succeeded; refreshing the library is the caller's
    // follow-up so a failed refresh cannot be reported as an import failure.
    selectedPaperIds.value = []
    return batch
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : t('sources.literatureImportFailed')
    throw cause
  } finally {
    importing.value = false
  }
}

export function useLiteratureDiscovery() {
  return {
    providers,
    page,
    searchExecutionId,
    selectedPaperIds,
    loadingProviders,
    searching,
    importing,
    error,
    selectedCount: computed(() => selectedPaperIds.value.length),
    allVisibleSelected: computed(
      () =>
        Boolean(page.value?.records.length) &&
        page.value?.records.every((record) => selectedPaperIds.value.includes(record.paper_id)),
    ),
    loadProviders,
    searchLiterature,
    setPaperSelected,
    selectAllVisible,
    importSelected,
    resetDiscoveryState,
  }
}

export function _resetLiteratureDiscoveryForTesting(): void {
  providers.value = []
  loadingProviders.value = false
  searching.value = false
  importing.value = false
  resetDiscoveryState()
}
