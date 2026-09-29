<template>
  <div v-if="parsed" class="literature-answer" data-testid="literature-answer">
    <div class="answer-status" :data-status="parsed.status" data-testid="literature-status">
      <strong>
        {{
          parsed.status === 'answered'
            ? t('sources.answerStatusAnswered')
            : t('sources.answerStatusInsufficient')
        }}
      </strong>
      <span v-if="parsed.insufficient_reason">
        {{ reasonLabel(parsed.insufficient_reason) }}
      </span>
    </div>

    <ul v-if="parsed.claims.length" class="claim-list">
      <li
        v-for="(claim, index) in parsed.claims"
        :key="`${claim.text}-${index}`"
        data-testid="literature-claim"
      >
        <p class="claim-text">{{ claim.text }}</p>
        <ul v-if="evidenceFor(claim).length" class="claim-evidence">
          <li
            v-for="item in evidenceFor(claim)"
            :key="item.evidence_id"
            data-testid="literature-evidence"
          >
            <button
              type="button"
              data-testid="literature-evidence-toggle"
              @click="toggle(item.evidence_id)"
            >
              {{ item.title || item.source_id }} ·
              {{ t('sources.answerEvidencePage', { page: item.page }) }} ·
              {{
                expanded.includes(item.evidence_id)
                  ? t('sources.answerEvidenceCollapse')
                  : t('sources.answerEvidenceExpand')
              }}
            </button>
            <blockquote
              v-if="expanded.includes(item.evidence_id)"
              data-testid="literature-evidence-quote"
            >
              <p v-if="item.context_before" class="evidence-context">{{ item.context_before }}</p>
              <p class="evidence-quote">{{ item.exact_quote }}</p>
              <p v-if="item.context_after" class="evidence-context">{{ item.context_after }}</p>
            </blockquote>
          </li>
        </ul>
      </li>
    </ul>

    <p v-if="parsed.rejected_claims.length" class="answer-note" data-testid="literature-rejected">
      {{ t('sources.answerRejectedSummary', { count: parsed.rejected_claims.length }) }}
      <span v-for="(item, index) in parsed.rejected_claims" :key="`${item.reason}-${index}`">
        · {{ reasonLabel(item.reason, 'rejectedReason') }}
      </span>
    </p>
    <p v-if="parsed.unresolved_count" class="answer-note" data-testid="literature-unresolved">
      {{ t('sources.answerUnresolvedSummary', { count: parsed.unresolved_count }) }}
    </p>
  </div>

  <pre v-else class="literature-raw" data-testid="literature-raw">{{ result }}</pre>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { i18n } from '../i18n'

interface LiteratureAnswerEvidence {
  evidence_id: string
  source_id: string
  title: string
  page: number
  exact_quote: string
  context_before: string
  context_after: string
}

interface LiteratureAnswerClaim {
  text: string
  evidence_ids: string[]
  evidence_status: string
}

interface LiteratureAnswerNote {
  text: string
  reason: string
}

interface LiteratureAnswerPayload {
  status: 'answered' | 'insufficient'
  insufficient_reason: string | null
  claims: LiteratureAnswerClaim[]
  evidence: LiteratureAnswerEvidence[]
  rejected_claims: LiteratureAnswerNote[]
  unresolved_count: number
}

const props = defineProps<{ result: string }>()
const { t } = useI18n()
const expanded = ref<string[]>([])

/**
 * Only a well-formed literature answer is rendered as citations; anything else
 * falls back to the raw tool result so nothing is hidden or rewritten.
 */
const parsed = computed<LiteratureAnswerPayload | null>(() => {
  try {
    const payload = JSON.parse(props.result) as Partial<LiteratureAnswerPayload>
    if (!payload || typeof payload !== 'object') return null
    if (payload.status !== 'answered' && payload.status !== 'insufficient') return null
    if (!Array.isArray(payload.claims) || !Array.isArray(payload.evidence)) return null
    return {
      status: payload.status,
      insufficient_reason: payload.insufficient_reason ?? null,
      claims: payload.claims,
      evidence: payload.evidence,
      rejected_claims: Array.isArray(payload.rejected_claims) ? payload.rejected_claims : [],
      unresolved_count: Number(payload.unresolved_count || 0),
    }
  } catch {
    return null
  }
})

function evidenceFor(claim: LiteratureAnswerClaim): LiteratureAnswerEvidence[] {
  const available = new Map((parsed.value?.evidence ?? []).map((item) => [item.evidence_id, item]))
  return (claim.evidence_ids ?? [])
    .map((evidenceId) => available.get(evidenceId))
    .filter((item): item is LiteratureAnswerEvidence => Boolean(item))
}

function toggle(evidenceId: string) {
  expanded.value = expanded.value.includes(evidenceId)
    ? expanded.value.filter((item) => item !== evidenceId)
    : [...expanded.value, evidenceId]
}

function reasonLabel(
  reason: string,
  group: 'insufficientReason' | 'rejectedReason' = 'insufficientReason',
): string {
  const key = `sources.${group}.${reason}`
  return i18n.global.te(key) ? t(key) : reason
}
</script>

<style scoped>
.literature-answer {
  display: grid;
  gap: 8px;
}
.answer-status {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  font-size: 11px;
  color: var(--c-text-3);
}
.answer-status strong {
  color: var(--c-text-1);
  font-size: 12px;
}
.answer-status[data-status='insufficient'] strong {
  color: var(--c-warn);
}
.claim-list,
.claim-evidence {
  display: grid;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.claim-text {
  margin: 0;
  color: var(--c-text-1);
  font-size: 12px;
  line-height: 1.7;
}
.claim-evidence button {
  padding: 0;
  border: none;
  background: none;
  color: var(--c-accent);
  font-size: 11px;
  text-align: left;
  cursor: pointer;
}
.claim-evidence blockquote {
  margin: 4px 0 0;
  padding: 8px 10px;
  border: 1px solid var(--c-border);
  border-radius: 8px;
  background: var(--c-panel);
}
.evidence-quote {
  margin: 0;
  color: var(--c-text-1);
  font-size: 11px;
  line-height: 1.7;
  white-space: pre-wrap;
}
.evidence-context {
  margin: 0;
  color: var(--c-text-3);
  font-size: 11px;
  line-height: 1.6;
  white-space: pre-wrap;
}
.answer-note {
  margin: 0;
  color: var(--c-warn);
  font-size: 11px;
  line-height: 1.7;
}
.literature-raw {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
