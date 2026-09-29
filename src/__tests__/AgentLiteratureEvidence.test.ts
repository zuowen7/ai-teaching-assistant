import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import AgentLiteratureEvidence from '../components/AgentLiteratureEvidence.vue'

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

const EVIDENCE_ID = `evidence_${'a'.repeat(24)}`

const answered = JSON.stringify({
  status: 'answered',
  insufficient_reason: null,
  question: 'What protocol?',
  source_ids: ['src_demo_0001'],
  claims: [
    {
      text: '评估使用了留出集。',
      evidence_ids: [EVIDENCE_ID],
      evidence_status: 'supported',
    },
    {
      text: '两篇论文的协议不同。',
      evidence_ids: [`evidence_${'b'.repeat(24)}`],
      evidence_status: 'conflicting',
    },
  ],
  evidence: [
    {
      evidence_id: EVIDENCE_ID,
      source_id: 'src_demo_0001',
      title: 'Demo Paper A',
      page: 2,
      exact_quote: 'Only this page mentions the held-out split.',
      context_before: 'before context',
      context_after: 'after context',
    },
  ],
  rejected_claims: [{ text: 'As shown on page 9.', reason: 'fabricated_page_reference' }],
  unresolved_count: 1,
})

// Deliberately violating payload: status says insufficient while claims and
// evidence are present.  The component must still render nothing as a conclusion.
const insufficient = JSON.stringify({
  status: 'insufficient',
  insufficient_reason: 'no_resolvable_evidence',
  question: 'What protocol?',
  source_ids: ['src_demo_0001'],
  claims: [
    {
      text: 'This claim must never be rendered.',
      evidence_ids: [EVIDENCE_ID],
      evidence_status: 'supported',
    },
  ],
  evidence: [
    {
      evidence_id: EVIDENCE_ID,
      source_id: 'src_demo_0001',
      title: 'Demo Paper A',
      page: 2,
      exact_quote: 'This quote must never be rendered.',
      context_before: '',
      context_after: '',
    },
  ],
  rejected_claims: [],
  unresolved_count: 2,
  next_actions: ['widen_scope_within_project', 'propose_new_query_for_confirmation'],
  must_not_answer_from_memory: true,
})

function mountWith(result: string) {
  return mount(AgentLiteratureEvidence, { props: { result } })
}

describe('AgentLiteratureEvidence', () => {
  it('renders each claim with its page-level citation', () => {
    const wrapper = mountWith(answered)

    const claims = wrapper.findAll('[data-testid="literature-claim"]')
    expect(claims).toHaveLength(2)
    expect(claims[0].text()).toContain('评估使用了留出集。')
    expect(claims[0].text()).toContain('Demo Paper A')
    expect(claims[0].text()).toContain('第 2 页')
    // A claim whose evidence was not returned keeps no empty citation card.
    expect(claims[1].findAll('[data-testid="literature-evidence"]')).toHaveLength(0)
  })

  it('shows the exact quote only after the citation is expanded', async () => {
    const wrapper = mountWith(answered)
    const toggle = wrapper.get('[data-testid="literature-evidence-toggle"]')

    expect(toggle.text()).toContain('展开原文')
    expect(wrapper.find('[data-testid="literature-evidence-quote"]').exists()).toBe(false)

    await toggle.trigger('click')

    const quote = wrapper.get('[data-testid="literature-evidence-quote"]')
    expect(quote.text()).toContain('Only this page mentions the held-out split.')
    expect(quote.text()).toContain('before context')
    expect(quote.text()).toContain('after context')
    expect(toggle.text()).toContain('收起原文')

    await toggle.trigger('click')
    expect(wrapper.find('[data-testid="literature-evidence-quote"]').exists()).toBe(false)
  })

  it('keeps rejected claims and unresolved counts visible', () => {
    const wrapper = mountWith(answered)

    expect(wrapper.get('[data-testid="literature-rejected"]').text()).toContain(
      '1 条结论未通过证据校验',
    )
    expect(wrapper.get('[data-testid="literature-unresolved"]').text()).toContain(
      '1 条检索片段无法核验',
    )
  })

  it('shows only the insufficiency reason when there is no evidence', () => {
    const wrapper = mountWith(insufficient)

    expect(wrapper.get('[data-testid="literature-status"]').text()).toContain('证据不足')
    expect(wrapper.get('[data-testid="literature-status"]').text()).toContain(
      '检索到的片段无法核验回页码与原文',
    )
    // The payload carries a claim and a quote; neither may reach the UI.
    expect(wrapper.findAll('[data-testid="literature-claim"]')).toHaveLength(0)
    expect(wrapper.find('[data-testid="literature-evidence"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('This claim must never be rendered.')
    expect(wrapper.text()).not.toContain('This quote must never be rendered.')
  })

  it('falls back to the raw tool result when it is not a literature answer', () => {
    const raw = 'not json at all'
    const wrapper = mountWith(raw)

    expect(wrapper.find('[data-testid="literature-answer"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="literature-raw"]').text()).toContain(raw)
  })

  it('falls back to the raw result when the payload has no claims field', () => {
    const wrapper = mountWith(JSON.stringify({ status: 'answered' }))

    expect(wrapper.find('[data-testid="literature-answer"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="literature-raw"]').text()).toContain('"answered"')
  })

  it('renders a real-shaped SSE payload larger than the old 4000 character cut', () => {
    const quote = 'evidence sentence. '.repeat(70)
    const evidence = [1, 2, 3].map((index) => ({
      evidence_id: `evidence_${String(index).padStart(24, '0')}`,
      source_id: `src_demo_${index}`,
      title: `Demo Paper ${index}`,
      page: index,
      exact_quote: quote,
      context_before: '',
      context_after: '',
    }))
    const payload = JSON.stringify({
      status: 'answered',
      insufficient_reason: null,
      claims: [
        {
          text: 'Three papers agree.',
          evidence_ids: evidence.map((item) => item.evidence_id),
          evidence_status: 'supported',
        },
      ],
      evidence,
      rejected_claims: [],
      unresolved_count: 0,
    })
    expect(payload.length).toBeGreaterThan(4_000)
    // The SSE adapter only shortens the collapsed `content` field, never the
    // expanded detail, so the component always receives the whole payload.
    const wrapper = mountWith(payload)

    expect(wrapper.find('[data-testid="literature-raw"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="literature-evidence"]')).toHaveLength(3)

    return wrapper
      .get('[data-testid="literature-evidence-toggle"]')
      .trigger('click')
      .then(() => {
        expect(wrapper.get('[data-testid="literature-evidence-quote"]').text()).toContain(
          quote.trim().slice(0, 40),
        )
      })
  })
})
