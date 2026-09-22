<template>
  <div class="source-library">
    <AppHeader
      :title="t('sources.title')"
      :subtitle="t('sources.subtitle', { count: library.sources.value.length })"
      :icon="Library"
    >
      <UiButton
        variant="primary"
        size="sm"
        data-testid="open-literature-discovery"
        @click="openLiteratureDiscovery"
      >
        <Search :size="14" /> {{ t('sources.discoverLiterature') }}
      </UiButton>
      <UiButton variant="secondary" size="sm" @click="openZotero">
        <BookOpen :size="14" /> {{ t('sources.fromZotero') }}
      </UiButton>
      <UiButton
        variant="secondary"
        size="sm"
        :loading="library.saving.value"
        @click="pickReference()"
      >
        <Plus :size="14" /> {{ t('sources.importFile') }}
      </UiButton>
    </AppHeader>

    <div
      v-if="translatingSourceId"
      class="job-banner"
      role="status"
      data-testid="source-translation-job"
    >
      <Languages :size="15" />
      <span>
        {{ t('sources.translatingSource', { title: translatingSource?.title || '' }) }}
      </span>
      <div class="job-progress"><i :style="{ width: `${translate.overallProgress()}%` }" /></div>
      <strong>{{ translate.overallProgress() }}%</strong>
    </div>

    <div v-if="library.error.value" class="library-error" role="alert">
      {{ library.error.value }}
    </div>

    <div class="library-workspace">
      <aside class="library-nav" :aria-label="t('sources.filters')">
        <strong>{{ t('sources.projectLibrary') }}</strong>
        <button
          v-for="option in filterOptions"
          :key="option.value"
          type="button"
          :class="{ active: filter === option.value }"
          @click="filter = option.value"
        >
          <component :is="option.icon" :size="15" />
          <span>{{ option.label }}</span>
          <b>{{ option.count }}</b>
        </button>
        <div class="nav-separator" />
        <span class="nav-caption">{{ t('sources.workflowStatus') }}</span>
        <div class="library-stat">
          <Database :size="14" />
          {{ t('sources.indexedCount', { count: indexedCount }) }}
        </div>
        <div class="library-stat">
          <Languages :size="14" />
          {{ t('sources.translatedCount', { count: library.translatedCount.value }) }}
        </div>
      </aside>

      <section class="source-browser">
        <div class="browser-toolbar">
          <label class="source-search">
            <Search :size="15" />
            <input v-model="searchQuery" :placeholder="t('sources.searchPlaceholder')" />
          </label>
          <span>{{ t('sources.resultCount', { count: filteredSources.length }) }}</span>
        </div>

        <div v-if="filteredSources.length" class="source-list">
          <button
            v-for="source in filteredSources"
            :key="source.id"
            type="button"
            class="source-row"
            :class="{ active: selectedSource?.id === source.id }"
            @click="selectSource(source)"
          >
            <div class="source-icon"><FileText :size="18" /></div>
            <span class="source-copy">
              <strong>{{ source.title }}</strong>
              <small>{{ sourceMetaLine(source) }}</small>
              <span class="source-badges">
                <i :class="`rag-${source.rag_status}`">{{ ragLabel(source.rag_status) }}</i>
                <i v-if="source.translation_task_id">{{ t('sources.translated') }}</i>
                <i v-if="source.cited">{{ t('sources.cited') }}</i>
              </span>
            </span>
          </button>
        </div>
        <EmptyState
          v-else-if="!library.loading.value"
          :title="searchQuery ? t('sources.noResults') : t('sources.emptyTitle')"
          :description="
            searchQuery ? t('sources.noResultsDescription') : t('sources.emptyDescription')
          "
        >
          <UiButton v-if="!searchQuery" variant="primary" size="sm" @click="pickReference()">
            {{ t('sources.importFile') }}
          </UiButton>
        </EmptyState>
        <div v-else class="loading-state">{{ t('general.loading') }}</div>
      </section>

      <aside class="source-detail">
        <template v-if="selectedSource">
          <div class="detail-heading">
            <div class="detail-icon"><FileText :size="20" /></div>
            <div>
              <span>{{ t('sources.sourceDetails') }}</span>
              <h2>{{ selectedSource.title }}</h2>
              <p>{{ sourceMetaLine(selectedSource) }}</p>
            </div>
          </div>

          <div class="action-grid">
            <UiButton
              variant="secondary"
              size="sm"
              :disabled="!selectedSource.original_path"
              @click="loadContent('original')"
            >
              <BookOpen :size="14" /> {{ t('sources.readOriginal') }}
            </UiButton>
            <UiButton
              v-if="!selectedSource.original_path"
              variant="secondary"
              size="sm"
              @click="pickReference(selectedSource)"
            >
              <Plus :size="14" /> {{ t('sources.attachFullText') }}
            </UiButton>
            <UiButton
              variant="secondary"
              size="sm"
              :disabled="!selectedSource.original_path || translatingSourceId !== ''"
              @click="startTranslation"
            >
              <Languages :size="14" /> {{ t('sources.translate') }}
            </UiButton>
            <UiButton
              variant="secondary"
              size="sm"
              :loading="library.indexingSourceId.value === selectedSource.id"
              :disabled="!selectedSource.original_path"
              @click="runIndex"
            >
              <Database :size="14" />
              {{
                selectedSource.rag_status === 'ready'
                  ? t('sources.reindex')
                  : t('sources.indexForRag')
              }}
            </UiButton>
            <UiButton
              v-if="selectedSource.translated_path"
              variant="secondary"
              size="sm"
              @click="loadContent('translated')"
            >
              <Languages :size="14" /> {{ t('sources.readTranslation') }}
            </UiButton>
            <UiButton variant="secondary" size="sm" @click="copyCitation">
              <Quote :size="14" /> {{ t('sources.copyCitation') }}
            </UiButton>
            <UiButton
              variant="ghost"
              size="sm"
              :loading="library.deletingSourceId.value === selectedSource.id"
              @click="removeSelected"
            >
              <Trash2 :size="14" /> {{ t('general.delete') }}
            </UiButton>
          </div>

          <section class="detail-section">
            <div class="section-title">
              <strong>{{ t('sources.tags') }}</strong>
              <button type="button" @click="saveTags">{{ t('general.save') }}</button>
            </div>
            <input
              v-model="tagDraft"
              class="tag-input"
              :placeholder="t('sources.tagsPlaceholder')"
              @keydown.enter.prevent="saveTags"
            />
          </section>

          <section v-if="selectedSource.rag_status === 'ready'" class="detail-section">
            <strong>{{ t('sources.askLibrary') }}</strong>
            <form class="rag-query" @submit.prevent="askRag">
              <input v-model="ragQuery" :placeholder="t('sources.askPlaceholder')" />
              <UiButton
                variant="primary"
                size="sm"
                type="submit"
                :loading="querying"
                :disabled="!ragQuery.trim()"
              >
                {{ t('sources.searchRag') }}
              </UiButton>
            </form>
            <div v-if="queryHits.length" class="query-results">
              <article v-for="hit in queryHits" :key="hit.chunk_id">
                <span>{{ hit.source }}</span>
                <p>{{ hit.text }}</p>
              </article>
            </div>
          </section>

          <section v-if="readerContent" class="reader-panel">
            <div class="section-title">
              <strong>
                {{
                  readerVersion === 'translated' ? t('sources.translation') : t('sources.original')
                }}
              </strong>
              <span>
                {{
                  t('sources.contentStats', {
                    pages: readerContent.pages,
                    chars: readerContent.chars,
                  })
                }}
              </span>
            </div>
            <pre>{{ readerContent.text }}</pre>
          </section>
        </template>
        <EmptyState
          v-else
          :title="t('sources.selectSource')"
          :description="t('sources.selectSourceDescription')"
        />
      </aside>
    </div>

    <div
      v-if="literatureOpen"
      class="zotero-overlay"
      data-testid="literature-discovery-dialog"
      @click.self="literatureOpen = false"
    >
      <section
        class="zotero-dialog literature-dialog"
        role="dialog"
        :aria-label="t('sources.literatureDiscovery')"
      >
        <div class="dialog-heading">
          <div>
            <span>{{ selectedLiteratureProvider }}</span>
            <h2>{{ t('sources.literatureDiscovery') }}</h2>
          </div>
          <button type="button" :aria-label="t('general.close')" @click="literatureOpen = false">
            <X :size="18" />
          </button>
        </div>

        <form
          class="literature-plan"
          data-testid="literature-search-form"
          @submit.prevent="executeLiteratureSearch"
        >
          <label class="literature-field question-field">
            <span>{{ t('sources.researchQuestion') }}</span>
            <textarea
              v-model="researchQuestion"
              rows="2"
              data-testid="literature-research-question"
              :placeholder="t('sources.researchQuestionPlaceholder')"
            />
          </label>
          <label class="literature-field provider-field">
            <span>{{ t('sources.literatureProvider') }}</span>
            <select v-model="selectedLiteratureProvider">
              <option
                v-for="provider in discovery.providers.value"
                :key="provider.provider"
                :value="provider.provider"
              >
                {{ provider.provider }}
              </option>
              <option v-if="!discovery.providers.value.length" value="arxiv">arXiv</option>
            </select>
          </label>
          <label class="literature-field query-field">
            <span>{{ t('sources.confirmedQuery') }}</span>
            <div class="query-editor">
              <input
                v-model="confirmedLiteratureQuery"
                data-testid="literature-confirmed-query"
                spellcheck="false"
                :placeholder="t('sources.confirmedQueryPlaceholder')"
              />
              <UiButton type="button" variant="secondary" size="sm" @click="buildLiteratureQuery">
                {{ t('sources.buildQuery') }}
              </UiButton>
            </div>
            <small>{{ t('sources.queryConfirmationHint') }}</small>
          </label>
          <div class="literature-plan-action">
            <UiButton
              type="submit"
              variant="primary"
              size="sm"
              :loading="discovery.searching.value"
              :disabled="!researchQuestion.trim() || !confirmedLiteratureQuery.trim()"
            >
              <Search :size="14" /> {{ t('sources.confirmAndSearch') }}
            </UiButton>
          </div>
        </form>

        <div v-if="discovery.error.value" class="literature-error" role="alert">
          {{ discovery.error.value }}
        </div>

        <template v-if="discovery.page.value">
          <div class="query-receipt" data-testid="literature-query-receipt">
            <div>
              <span>{{ t('sources.searchReceipt') }}</span>
              <b :class="`mode-${discovery.page.value.result_mode}`">
                {{ t(`sources.resultMode.${discovery.page.value.result_mode}`) }}
              </b>
            </div>
            <code>{{ discovery.page.value.query.query }}</code>
            <small>
              {{
                t('sources.searchResultSummary', {
                  returned: discovery.page.value.records.length,
                  total: discovery.page.value.total_results ?? '—',
                })
              }}
            </small>
          </div>

          <div class="literature-selection-bar">
            <label>
              <input
                type="checkbox"
                :checked="discovery.allVisibleSelected.value"
                :disabled="!discovery.page.value.records.length"
                @change="toggleAllLiteratureRecords"
              />
              {{ t('sources.selectAllResults') }}
            </label>
            <span>{{
              t('sources.selectedResultCount', { count: discovery.selectedCount.value })
            }}</span>
          </div>

          <div v-if="discovery.page.value.records.length" class="literature-results">
            <label
              v-for="record in discovery.page.value.records"
              :key="record.paper_id"
              class="literature-result"
              :class="{ selected: discovery.selectedPaperIds.value.includes(record.paper_id) }"
            >
              <input
                type="checkbox"
                :data-testid="`literature-select-${record.paper_id}`"
                :checked="discovery.selectedPaperIds.value.includes(record.paper_id)"
                @change="toggleLiteratureRecord(record.paper_id, $event)"
              />
              <span class="literature-result-copy">
                <strong>{{ record.title }}</strong>
                <small>
                  {{
                    [record.authors.join(', '), record.year, record.venue]
                      .filter(Boolean)
                      .join(' · ')
                  }}
                </small>
                <p>{{ record.abstract || t('sources.noAbstract') }}</p>
              </span>
              <span class="literature-result-side">
                <i :class="`access-${literatureAccessStatus(record)}`">
                  {{ t(`sources.access.${literatureAccessStatus(record)}`) }}
                </i>
                <a :href="record.record_url" target="_blank" rel="noopener" @click.stop>
                  {{ t('sources.viewRecord') }}
                </a>
              </span>
            </label>
          </div>
          <EmptyState
            v-else
            :title="t('sources.noLiteratureResults')"
            :description="t('sources.noLiteratureResultsDescription')"
          />

          <footer class="literature-footer">
            <span>{{ t('sources.snapshotImportHint') }}</span>
            <UiButton
              type="button"
              variant="primary"
              size="sm"
              data-testid="literature-import-selected"
              :loading="discovery.importing.value"
              :disabled="!discovery.selectedCount.value"
              @click="importLiteratureSelection"
            >
              <Plus :size="14" />
              {{ t('sources.importSelected', { count: discovery.selectedCount.value }) }}
            </UiButton>
          </footer>
        </template>
      </section>
    </div>

    <div v-if="zoteroOpen" class="zotero-overlay" @click.self="zoteroOpen = false">
      <section class="zotero-dialog" role="dialog" :aria-label="t('sources.fromZotero')">
        <div class="dialog-heading">
          <div>
            <span>Zotero</span>
            <h2>{{ t('sources.importFromZotero') }}</h2>
          </div>
          <button type="button" :aria-label="t('general.close')" @click="zoteroOpen = false">
            <X :size="18" />
          </button>
        </div>
        <form class="zotero-search" @submit.prevent="searchZoteroItems">
          <input v-model="zoteroQuery" :placeholder="t('sources.zoteroSearchPlaceholder')" />
          <UiButton type="submit" variant="primary" size="sm" :loading="zoteroSearching">
            {{ t('general.search') }}
          </UiButton>
        </form>
        <div class="zotero-results">
          <article v-for="item in zoteroItems" :key="item.key">
            <div>
              <strong>{{ item.title || item.citation_key || item.key }}</strong>
              <span>{{
                [item.authors?.join(', '), item.year, item.journal].filter(Boolean).join(' · ')
              }}</span>
            </div>
            <UiButton variant="secondary" size="sm" @click="addZoteroItem(item)">
              {{ t('sources.addToProject') }}
            </UiButton>
          </article>
          <EmptyState
            v-if="zoteroSearched && !zoteroItems.length"
            :title="t('sources.zoteroNoResults')"
            :description="t('sources.zoteroNoResultsDescription')"
          />
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  BookOpen,
  CheckCircle2,
  Database,
  FileText,
  Languages,
  Library,
  List,
  Plus,
  Quote,
  Search,
  Trash2,
  X,
} from 'lucide-vue-next'
import { open as openDialog } from '@tauri-apps/plugin-dialog'
import { useI18n } from 'vue-i18n'
import AppHeader from './shell/AppHeader.vue'
import EmptyState from './shell/EmptyState.vue'
import UiButton from './ui/UiButton.vue'
import {
  useSourceLibrary,
  type ProjectSource,
  type SourceContent,
  type SourceQueryHit,
  type SourceRagStatus,
} from '../composables/useSourceLibrary'
import { useTranslate } from '../composables/useTranslate'
import { useToast } from '../composables/useToast'
import { useFileTree } from '../composables/useFileTree'
import { useEditorCitation, type ZoteroItem } from '../composables/useEditorCitation'
import {
  suggestArxivQuery,
  useLiteratureDiscovery,
  type LiteraturePaperRecord,
} from '../composables/useLiteratureDiscovery'

defineProps<{
  healthOk: boolean
  backendRestarting: boolean
  readSettings: { fontSize: number; lineHeight: number; fontFamily: string; transColor: string }
}>()
const emit = defineEmits<{
  (event: 'restart-backend'): void
  (event: 'open-agent-docs'): void
  (event: 'open-settings', tab: 'integrations'): void
}>()

type LibraryFilter = 'all' | 'unread' | 'translated' | 'indexed' | 'cited'

const { t } = useI18n()
const { pushError, success } = useToast()
const library = useSourceLibrary()
const discovery = useLiteratureDiscovery()
const translate = useTranslate()
const citation = useEditorCitation()
const filter = ref<LibraryFilter>('all')
const searchQuery = ref('')
const selectedSourceId = ref('')
const tagDraft = ref('')
const readerContent = ref<SourceContent | null>(null)
const readerVersion = ref<'original' | 'translated'>('original')
const ragQuery = ref('')
const queryHits = ref<SourceQueryHit[]>([])
const querying = ref(false)
const translatingSourceId = ref('')
const zoteroOpen = ref(false)
const zoteroQuery = ref('')
const zoteroItems = ref<ZoteroItem[]>([])
const zoteroSearching = ref(false)
const zoteroSearched = ref(false)
const literatureOpen = ref(false)
const researchQuestion = ref('')
const suggestedLiteratureQuery = ref('')
const confirmedLiteratureQuery = ref('')
const selectedLiteratureProvider = ref('arxiv')

const selectedSource = computed(
  () => library.sources.value.find((source) => source.id === selectedSourceId.value) ?? null,
)
const translatingSource = computed(
  () => library.sources.value.find((source) => source.id === translatingSourceId.value) ?? null,
)
const indexedCount = computed(
  () => library.sources.value.filter((source) => source.rag_status === 'ready').length,
)
const filterOptions = computed(() => [
  {
    value: 'all' as const,
    label: t('general.all'),
    icon: List,
    count: library.sources.value.length,
  },
  {
    value: 'unread' as const,
    label: t('sources.unread'),
    icon: BookOpen,
    count: library.sources.value.filter((source) => source.reading_status !== 'read').length,
  },
  {
    value: 'translated' as const,
    label: t('sources.translated'),
    icon: Languages,
    count: library.translatedCount.value,
  },
  {
    value: 'indexed' as const,
    label: t('sources.indexed'),
    icon: Database,
    count: indexedCount.value,
  },
  {
    value: 'cited' as const,
    label: t('sources.cited'),
    icon: CheckCircle2,
    count: library.citedCount.value,
  },
])
const filteredSources = computed(() => {
  const query = searchQuery.value.trim().toLocaleLowerCase()
  return library.sources.value.filter((source) => {
    if (filter.value === 'unread' && source.reading_status === 'read') return false
    if (filter.value === 'translated' && !source.translation_task_id) return false
    if (filter.value === 'indexed' && source.rag_status !== 'ready') return false
    if (filter.value === 'cited' && !source.cited) return false
    if (!query) return true
    const metadata = JSON.stringify(source.metadata).toLocaleLowerCase()
    return source.title.toLocaleLowerCase().includes(query) || metadata.includes(query)
  })
})

onMounted(async () => {
  await library.loadSources().catch(() => undefined)
  selectedSourceId.value = library.sources.value[0]?.id ?? ''
})

watch(selectedSource, (source) => {
  const tags = Array.isArray(source?.metadata.tags) ? source?.metadata.tags : []
  tagDraft.value = tags.filter((tag): tag is string => typeof tag === 'string').join(', ')
})

watch(researchQuestion, () => {
  suggestedLiteratureQuery.value = ''
})

// The view is kept alive across project switches, so a snapshot discovered for
// one project must not stay importable into another one.
watch(
  () => useFileTree().rootDir.value,
  () => {
    discovery.resetDiscoveryState()
    researchQuestion.value = ''
    suggestedLiteratureQuery.value = ''
    confirmedLiteratureQuery.value = ''
  },
)

function ragLabel(status: SourceRagStatus) {
  return t(`sources.rag.${status}`)
}

function sourceMetaLine(source: ProjectSource): string {
  const authors = Array.isArray(source.metadata.authors)
    ? source.metadata.authors
        .filter((value): value is string => typeof value === 'string')
        .join(', ')
    : ''
  const values = [
    authors,
    typeof source.metadata.year === 'string' ? source.metadata.year : '',
    typeof source.metadata.journal === 'string' ? source.metadata.journal : '',
    typeof source.metadata.pages === 'number'
      ? t('sources.pageCount', { count: source.metadata.pages })
      : '',
  ].filter(Boolean)
  return values.join(' · ') || t('sources.localAttachment')
}

function selectSource(source: ProjectSource) {
  selectedSourceId.value = source.id
  readerContent.value = null
  queryHits.value = []
}

async function importPickedFile(file: File, existingSource?: ProjectSource) {
  try {
    const source = await library.importSource(file, existingSource)
    selectedSourceId.value = source.id
    success(t('sources.added'))
  } catch (cause) {
    pushError(cause instanceof Error ? cause.message : t('sources.addFailed'))
  }
}

async function pickReference(existingSource?: ProjectSource) {
  try {
    const selected = await openDialog({
      multiple: false,
      filters: [
        {
          name: 'Academic documents',
          extensions: ['pdf', 'docx', 'doc', 'md', 'txt', 'tex', 'html', 'epub', 'rtf'],
        },
      ],
    })
    if (typeof selected === 'string') {
      const { readFile } = await import('@tauri-apps/plugin-fs')
      const bytes = await readFile(selected)
      await importPickedFile(
        new File([bytes], selected.split(/[\\/]/).pop() || 'source'),
        existingSource,
      )
      return
    }
    if (selected === null) return
  } catch {
    // Browser preview uses a standard file picker.
  }
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.pdf,.docx,.doc,.md,.txt,.tex,.html,.epub,.rtf'
  input.onchange = () => {
    const file = input.files?.[0]
    if (file) void importPickedFile(file, existingSource)
  }
  input.click()
}

async function loadContent(version: 'original' | 'translated') {
  if (!selectedSource.value) return
  try {
    readerContent.value = await library.readSource(selectedSource.value, version)
    readerVersion.value = version
  } catch (cause) {
    pushError(cause instanceof Error ? cause.message : t('sources.readFailed'))
  }
}

async function runIndex() {
  if (!selectedSource.value) return
  try {
    const source = await library.indexSource(selectedSource.value)
    selectedSourceId.value = source.id
    success(t('sources.indexReady'))
  } catch (cause) {
    pushError(cause instanceof Error ? cause.message : t('sources.indexFailed'))
  }
}

async function askRag() {
  if (!selectedSource.value || !ragQuery.value.trim()) return
  querying.value = true
  try {
    queryHits.value = await library.querySources(ragQuery.value.trim(), [selectedSource.value.id])
    if (!queryHits.value.length) pushError(t('sources.noGroundedResults'))
  } catch (cause) {
    pushError(cause instanceof Error ? cause.message : t('sources.queryFailed'))
  } finally {
    querying.value = false
  }
}

async function startTranslation() {
  const source = selectedSource.value
  if (!source?.original_path) return
  translatingSourceId.value = source.id
  await translate.translateFromPath(source.original_path)
  try {
    if (translate.state.status !== 'done') {
      throw new Error(translate.state.errorMessage || t('sources.translationFailed'))
    }
    let saved = await library.attachTranslationToSource(source)
    selectedSourceId.value = saved.id
    success(t('sources.translationAttached'))
    try {
      saved = await library.indexSource(saved)
      selectedSourceId.value = saved.id
      success(t('sources.indexReady'))
    } catch (cause) {
      pushError(cause instanceof Error ? cause.message : t('sources.indexFailed'))
    }
  } catch (cause) {
    pushError(cause instanceof Error ? cause.message : t('sources.translationFailed'))
  } finally {
    translatingSourceId.value = ''
  }
}

async function saveTags() {
  if (!selectedSource.value) return
  const tags = [
    ...new Set(
      tagDraft.value
        .split(/[,，]/)
        .map((tag) => tag.trim())
        .filter(Boolean),
    ),
  ]
  try {
    await library.updateSource(selectedSource.value, {
      metadata: { ...selectedSource.value.metadata, tags },
    })
    success(t('general.saved'))
  } catch (cause) {
    pushError(cause instanceof Error ? cause.message : t('general.saveFailed'))
  }
}

async function copyCitation() {
  if (!selectedSource.value) return
  const metadata = selectedSource.value.metadata
  const key =
    (typeof metadata.citation_key === 'string' && metadata.citation_key) ||
    (typeof metadata.zotero_key === 'string' && metadata.zotero_key) ||
    selectedSource.value.id
  const text =
    (typeof metadata.markdown_citation === 'string' && metadata.markdown_citation) || `[@${key}]`
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const textarea = document.createElement('textarea')
    textarea.value = text
    document.body.appendChild(textarea)
    textarea.select()
    document.execCommand('copy')
    textarea.remove()
  }
  await library.updateSource(selectedSource.value, { cited: true })
  success(t('sources.citationCopied'))
}

async function removeSelected() {
  const source = selectedSource.value
  if (!source || !window.confirm(t('sources.deleteConfirm', { title: source.title }))) return
  try {
    await library.deleteSource(source)
    selectedSourceId.value = filteredSources.value[0]?.id ?? ''
    readerContent.value = null
    success(t('sources.deleted'))
  } catch (cause) {
    pushError(cause instanceof Error ? cause.message : t('sources.deleteFailed'))
  }
}

async function openLiteratureDiscovery() {
  literatureOpen.value = true
  if (discovery.providers.value.length) return
  try {
    const providers = await discovery.loadProviders()
    if (!providers.some((provider) => provider.provider === selectedLiteratureProvider.value)) {
      selectedLiteratureProvider.value = providers[0]?.provider ?? 'arxiv'
    }
  } catch (cause) {
    pushError(cause instanceof Error ? cause.message : t('sources.providerLoadFailed'))
  }
}

function buildLiteratureQuery() {
  const query = suggestArxivQuery(researchQuestion.value)
  if (!query) {
    pushError(t('sources.researchQuestionRequired'))
    return
  }
  suggestedLiteratureQuery.value = query
  confirmedLiteratureQuery.value = query
}

async function executeLiteratureSearch() {
  if (!researchQuestion.value.trim()) {
    pushError(t('sources.researchQuestionRequired'))
    return
  }
  if (!confirmedLiteratureQuery.value.trim()) {
    pushError(t('sources.confirmedQueryRequired'))
    return
  }
  const generatedFromTemplate = Boolean(suggestedLiteratureQuery.value)
  try {
    await discovery.searchLiterature(
      selectedLiteratureProvider.value,
      confirmedLiteratureQuery.value,
      {
        researchQuestion: researchQuestion.value,
        suggestedQuery: suggestedLiteratureQuery.value || confirmedLiteratureQuery.value,
        generationMethod: generatedFromTemplate ? 'template' : 'user',
        generationModel: null,
        generationConfig: generatedFromTemplate ? { template: 'arxiv_all_phrase_v1' } : {},
      },
    )
  } catch (cause) {
    pushError(cause instanceof Error ? cause.message : t('sources.literatureSearchFailed'))
  }
}

function toggleLiteratureRecord(paperId: string, event: Event) {
  discovery.setPaperSelected(paperId, (event.target as HTMLInputElement).checked)
}

function toggleAllLiteratureRecords(event: Event) {
  discovery.selectAllVisible((event.target as HTMLInputElement).checked)
}

function literatureAccessStatus(record: LiteraturePaperRecord) {
  if (record.access_locations.some((location) => location.access_status === 'open')) return 'open'
  if (record.access_locations.some((location) => location.access_status === 'restricted')) {
    return 'restricted'
  }
  return 'unknown'
}

async function importLiteratureSelection() {
  try {
    const batch = await discovery.importSelected()
    selectedSourceId.value = batch.results[0]?.source_id ?? selectedSourceId.value
    await library.loadSources().catch(() => undefined)
    success(
      t('sources.literatureImported', {
        created: batch.created_count,
        reused: batch.reused_count,
        updated: batch.metadata_updated_count,
      }),
    )
  } catch (cause) {
    pushError(cause instanceof Error ? cause.message : t('sources.literatureImportFailed'))
  }
}

async function openZotero() {
  try {
    const status = await citation.getZoteroStatus()
    if (!status?.connected) {
      pushError(status?.message || t('sources.zoteroNotConnected'))
      emit('open-settings', 'integrations')
      return
    }
    zoteroOpen.value = true
  } catch (cause) {
    pushError(cause instanceof Error ? cause.message : t('sources.zoteroNotConnected'))
  }
}

async function searchZoteroItems() {
  zoteroSearching.value = true
  try {
    zoteroItems.value = await citation.searchZotero(zoteroQuery.value.trim(), 30)
    zoteroSearched.value = true
  } finally {
    zoteroSearching.value = false
  }
}

async function addZoteroItem(item: ZoteroItem) {
  try {
    const source = await library.importZoteroItem(item)
    selectedSourceId.value = source.id
    success(t('sources.added'))
  } catch (cause) {
    pushError(cause instanceof Error ? cause.message : t('sources.addFailed'))
  }
}
</script>

<style scoped>
.source-library {
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--c-app-bg);
}
.job-banner,
.library-error {
  min-height: 36px;
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 6px 18px;
  border-bottom: 1px solid var(--c-border);
  font-size: 12px;
}
.job-banner {
  color: var(--c-accent);
  background: var(--c-accent-soft);
}
.job-banner span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.job-progress {
  width: min(180px, 22vw);
  height: 4px;
  margin-left: auto;
  overflow: hidden;
  border-radius: 999px;
  background: var(--c-surface-3);
}
.job-progress i {
  display: block;
  height: 100%;
  background: var(--c-accent);
  transition: width 180ms ease;
}
.library-error {
  color: var(--c-danger);
  background: var(--c-danger-bg);
}
.library-workspace {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-columns: 188px minmax(260px, 0.9fr) minmax(330px, 1.25fr);
}
.library-nav,
.source-browser,
.source-detail {
  min-width: 0;
  min-height: 0;
}
.library-nav {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 18px 10px;
  overflow: auto;
  border-right: 1px solid var(--c-border);
  background: var(--c-panel);
}
.library-nav > strong,
.nav-caption {
  padding: 0 9px 8px;
  color: var(--c-text-3);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.library-nav > button {
  width: 100%;
  min-height: 36px;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 9px;
  padding: 0 10px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--c-text-2);
  text-align: left;
  cursor: pointer;
}
.library-nav > button:hover,
.library-nav > button.active {
  background: var(--c-accent-soft);
  color: var(--c-accent);
}
.library-nav > button b {
  color: var(--c-text-3);
  font-size: 10px;
}
.nav-separator {
  height: 1px;
  margin: 12px 8px;
  background: var(--c-border);
}
.library-stat {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 9px;
  color: var(--c-text-3);
  font-size: 11px;
}
.source-browser {
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--c-border);
  background: var(--c-surface-1);
}
.browser-toolbar {
  min-height: 54px;
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--c-border);
  color: var(--c-text-3);
  font-size: 11px;
}
.source-search {
  min-width: 0;
  flex: 1;
  height: 34px;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 0 10px;
  border: 1px solid var(--c-border);
  border-radius: 8px;
  background: var(--c-panel);
  color: var(--c-text-3);
}
.source-search:focus-within {
  border-color: var(--c-accent-ring);
  box-shadow: 0 0 0 3px var(--c-accent-soft);
}
.source-search input,
.tag-input,
.rag-query input,
.zotero-search input {
  min-width: 0;
  flex: 1;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--c-text-1);
}
.source-list {
  min-height: 0;
  overflow: auto;
}
.source-row {
  width: 100%;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 11px;
  padding: 14px 13px;
  border: 0;
  border-bottom: 1px solid var(--c-border);
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.source-row:hover {
  background: var(--c-surface-2);
}
.source-row.active {
  background: var(--c-accent-soft);
  box-shadow: inset 3px 0 0 var(--c-accent);
}
.source-icon,
.detail-icon {
  display: grid;
  place-items: center;
  border-radius: 8px;
  background: var(--paper-0);
  color: var(--c-accent);
  box-shadow: inset 0 0 0 1px var(--c-border);
}
.source-icon {
  width: 34px;
  height: 40px;
}
.source-copy {
  min-width: 0;
  display: grid;
  gap: 5px;
}
.source-copy strong {
  overflow: hidden;
  color: var(--c-text-0);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-copy small {
  overflow: hidden;
  color: var(--c-text-3);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.source-badges i {
  padding: 2px 6px;
  border-radius: 999px;
  background: var(--c-surface-3);
  color: var(--c-text-3);
  font-size: 9px;
  font-style: normal;
}
.source-badges .rag-ready {
  background: var(--c-success-bg);
  color: var(--c-success);
}
.source-badges .rag-failed {
  background: var(--c-danger-bg);
  color: var(--c-danger);
}
.source-badges .rag-queued {
  background: var(--c-warn-bg);
  color: var(--c-warn);
}
.source-detail {
  overflow: auto;
  padding: 20px;
  background: var(--c-app-bg);
}
.detail-heading {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 13px;
  align-items: start;
}
.detail-icon {
  width: 42px;
  height: 48px;
}
.detail-heading span {
  color: var(--c-text-3);
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.detail-heading h2 {
  margin: 4px 0 5px;
  color: var(--c-text-0);
  font-family: var(--font-serif);
  font-size: 18px;
  line-height: 1.3;
}
.detail-heading p {
  margin: 0;
  color: var(--c-text-3);
  font-size: 11px;
}
.action-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 18px;
}
.detail-section,
.reader-panel {
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid var(--c-border);
}
.detail-section > strong,
.section-title strong {
  color: var(--c-text-1);
  font-size: 12px;
}
.section-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 9px;
}
.section-title button {
  border: 0;
  background: transparent;
  color: var(--c-accent);
  font-size: 11px;
  cursor: pointer;
}
.section-title span {
  color: var(--c-text-3);
  font-size: 10px;
}
.tag-input {
  width: 100%;
  height: 34px;
  margin-top: 9px;
  padding: 0 10px;
  border: 1px solid var(--c-border);
  border-radius: 8px;
  background: var(--c-panel);
}
.rag-query,
.zotero-search {
  display: flex;
  gap: 8px;
  margin-top: 9px;
}
.rag-query input,
.zotero-search input {
  height: 34px;
  padding: 0 10px;
  border: 1px solid var(--c-border);
  border-radius: 8px;
  background: var(--c-panel);
}
.query-results {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}
.query-results article {
  padding: 10px;
  border: 1px solid var(--c-border);
  border-radius: 8px;
  background: var(--c-panel);
}
.query-results span {
  color: var(--c-accent);
  font-size: 10px;
}
.query-results p {
  margin: 5px 0 0;
  color: var(--c-text-2);
  font-size: 11px;
  line-height: 1.6;
}
.reader-panel pre {
  max-height: 42vh;
  margin: 0;
  padding: 15px;
  overflow: auto;
  border: 1px solid var(--c-border);
  border-radius: 9px;
  background: var(--paper-0);
  color: #2f2a22;
  font-family: var(--font-serif);
  font-size: 12px;
  line-height: 1.75;
  white-space: pre-wrap;
}
.loading-state {
  padding: 32px;
  color: var(--c-text-3);
  text-align: center;
}
.literature-dialog {
  width: min(860px, 100%);
}
.literature-plan {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 180px;
  gap: 12px;
  padding: 4px 20px 16px;
}
.literature-field {
  min-width: 0;
  display: grid;
  gap: 6px;
}
.literature-field > span {
  color: var(--c-text-2);
  font-size: 11px;
  font-weight: 650;
}
.literature-field textarea,
.literature-field select,
.query-editor input {
  width: 100%;
  border: 1px solid var(--c-border);
  border-radius: 8px;
  outline: 0;
  background: var(--c-surface-1);
  color: var(--c-text-1);
  font: inherit;
}
.literature-field textarea {
  min-height: 62px;
  padding: 9px 10px;
  resize: vertical;
  line-height: 1.45;
}
.literature-field select,
.query-editor input {
  height: 36px;
  padding: 0 10px;
}
.literature-field textarea:focus,
.literature-field select:focus,
.query-editor input:focus {
  border-color: var(--c-accent-ring);
  box-shadow: 0 0 0 3px var(--c-accent-soft);
}
.query-field {
  grid-column: 1 / -1;
}
.query-editor {
  display: flex;
  gap: 8px;
}
.query-editor input,
.query-receipt code {
  font-family: var(--font-mono);
}
.literature-field small,
.query-receipt small,
.literature-footer > span {
  color: var(--c-text-3);
  font-size: 10px;
  line-height: 1.5;
}
.literature-plan-action {
  grid-column: 1 / -1;
  display: flex;
  justify-content: flex-end;
}
.literature-error {
  margin: 0 20px 12px;
  padding: 9px 11px;
  border: 1px solid var(--c-danger);
  border-radius: 8px;
  background: var(--c-danger-bg);
  color: var(--c-danger);
  font-size: 11px;
}
.query-receipt {
  display: grid;
  gap: 6px;
  margin: 0 20px 12px;
  padding: 10px 12px;
  border: 1px solid var(--c-border);
  border-left: 3px solid var(--c-accent);
  border-radius: 7px;
  background: var(--paper-0);
}
.query-receipt > div {
  display: flex;
  align-items: center;
  gap: 8px;
}
/* The receipt is a paper-coloured panel in both themes, so its badge and label
   colours must be paper-safe ink instead of the surface-oriented --c-* tokens:
   --c-success/--c-warn on --paper-0 measured ~1.5:1 in the dark token set. */
.query-receipt > div > span {
  color: #4a4438;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.query-receipt b,
.literature-result-side i {
  padding: 2px 6px;
  border-radius: 999px;
  font-size: 9px;
  font-style: normal;
  font-weight: 650;
}
.query-receipt small {
  color: #4a4438;
}
.query-receipt .mode-live {
  background: rgba(49, 148, 90, 0.16);
  color: #1f5a34;
}
.query-receipt .mode-cache,
.query-receipt .mode-fixture {
  background: rgba(196, 122, 27, 0.18);
  color: #7a4d0c;
}
.literature-result-side .access-open {
  background: var(--c-success-bg);
  color: var(--c-success);
}
.literature-result-side .access-restricted {
  background: var(--c-warn-bg);
  color: var(--c-warn);
}
.literature-result-side .access-unknown {
  background: var(--c-surface-3);
  color: var(--c-text-3);
}
.query-receipt code {
  overflow-wrap: anywhere;
  color: #2f2a22;
  font-size: 11px;
}
.literature-selection-bar,
.literature-footer {
  min-height: 42px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 20px;
  border-top: 1px solid var(--c-border);
  color: var(--c-text-3);
  font-size: 11px;
}
.literature-selection-bar label {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--c-text-2);
}
.literature-selection-bar input,
.literature-result > input {
  accent-color: var(--c-accent);
}
.literature-results {
  min-height: 160px;
  max-height: 350px;
  overflow: auto;
  border-top: 1px solid var(--c-border);
}
.literature-result {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) 92px;
  gap: 11px;
  align-items: start;
  padding: 13px 20px;
  border-bottom: 1px solid var(--c-border);
  background: transparent;
  cursor: pointer;
}
.literature-result:hover,
.literature-result.selected {
  background: var(--c-accent-soft);
}
.literature-result > input {
  margin-top: 3px;
}
.literature-result-copy {
  min-width: 0;
  display: grid;
  gap: 4px;
}
.literature-result-copy strong {
  color: var(--c-text-1);
  font-family: var(--font-serif);
  font-size: 13px;
  line-height: 1.35;
}
.literature-result-copy small {
  overflow: hidden;
  color: var(--c-text-3);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.literature-result-copy p {
  display: -webkit-box;
  margin: 2px 0 0;
  overflow: hidden;
  color: var(--c-text-2);
  font-size: 10px;
  line-height: 1.5;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
.literature-result-side {
  display: grid;
  justify-items: end;
  gap: 9px;
}
.literature-result-side a {
  color: var(--c-accent);
  font-size: 10px;
  text-decoration: none;
}
.literature-result-side a:hover {
  text-decoration: underline;
}
.literature-footer {
  margin-top: auto;
}
.zotero-overlay {
  position: fixed;
  z-index: 1000;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(17, 20, 24, 0.46);
  backdrop-filter: blur(8px);
}
.zotero-dialog {
  width: min(680px, 100%);
  max-height: min(720px, calc(100vh - 40px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--c-border);
  border-radius: 14px;
  background: var(--c-panel);
  box-shadow: var(--elevation-4);
}
.dialog-heading {
  display: flex;
  align-items: start;
  justify-content: space-between;
  padding: 18px 20px 10px;
}
.dialog-heading span {
  color: var(--c-accent);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.dialog-heading h2 {
  margin: 4px 0 0;
  color: var(--c-text-0);
  font-size: 18px;
}
.dialog-heading > button {
  border: 0;
  background: transparent;
  color: var(--c-text-2);
  cursor: pointer;
}
.zotero-search {
  padding: 0 20px 15px;
}
.zotero-results {
  min-height: 180px;
  overflow: auto;
  border-top: 1px solid var(--c-border);
}
.zotero-results article {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--c-border);
}
.zotero-results article > div {
  min-width: 0;
  flex: 1;
  display: grid;
  gap: 4px;
}
.zotero-results strong {
  color: var(--c-text-1);
  font-size: 12px;
}
.zotero-results span {
  color: var(--c-text-3);
  font-size: 10px;
}
@media (max-width: 1100px) {
  .library-workspace {
    grid-template-columns: 164px minmax(260px, 0.85fr) minmax(300px, 1.15fr);
  }
  .source-detail {
    padding: 16px;
  }
}
@media (max-width: 860px) {
  .library-workspace {
    grid-template-columns: 150px minmax(250px, 0.9fr) minmax(280px, 1.1fr);
  }
  .library-nav {
    padding-inline: 7px;
  }
}
@media (max-width: 700px) {
  .library-workspace {
    display: flex;
    flex-direction: column;
    overflow: auto;
  }
  .library-nav {
    flex: 0 0 auto;
    flex-direction: row;
    padding: 7px;
    overflow-x: auto;
    border-right: 0;
    border-bottom: 1px solid var(--c-border);
  }
  .library-nav > strong,
  .nav-separator,
  .nav-caption,
  .library-stat {
    display: none;
  }
  .library-nav > button {
    width: auto;
    min-width: max-content;
    grid-template-columns: auto auto auto;
  }
  .source-browser {
    min-height: 260px;
    flex: 0 0 42%;
    border-right: 0;
    border-bottom: 1px solid var(--c-border);
  }
  .source-detail {
    min-height: 380px;
    flex: 1 0 auto;
    overflow: visible;
  }
  .job-progress {
    display: none;
  }
  .literature-plan {
    grid-template-columns: 1fr;
  }
  .provider-field,
  .query-field,
  .literature-plan-action {
    grid-column: 1;
  }
  .literature-result {
    grid-template-columns: auto minmax(0, 1fr);
  }
  .literature-result-side {
    grid-column: 2;
    grid-template-columns: auto auto;
    justify-content: start;
    justify-items: start;
  }
  .literature-footer {
    align-items: flex-start;
    flex-direction: column;
  }
}
@media (max-height: 650px) {
  .library-nav {
    padding-top: 10px;
  }
  .source-detail {
    padding-top: 14px;
  }
  .detail-section,
  .reader-panel {
    margin-top: 12px;
    padding-top: 12px;
  }
}
</style>
