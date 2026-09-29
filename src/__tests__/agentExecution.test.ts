import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

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
      t: (key: string, params?: Record<string, number | string>) => {
        if (key === 'agent.execution.title') return '执行过程'
        if (key === 'agent.execution.completed') return `已完成 · ${params?.count} 步`
        if (key === 'agent.execution.failed')
          return `共 ${params?.count} 步 · ${params?.failed} 步失败`
        if (key === 'agent.execution.readFile') return `读取 ${params?.target}`
        if (key === 'agent.execution.editFile') return `修改 ${params?.target}`
        return params ? composer.global.t(key, params) : composer.global.t(key)
      },
    }),
  }
})

import AgentExecutionGroup from '../components/AgentExecutionGroup.vue'
import type { AgentEvent } from '../types'
import {
  buildExecutionSteps,
  executionSummary,
  hasPendingApproval,
  hasRecoverablePartial,
} from '../utils/agentExecution'

describe('agent execution presentation', () => {
  const events: AgentEvent[] = [
    {
      type: 'tool_call',
      content: 'read_file',
      event_id: 'read-1',
      metadata: { tool_name: 'read_file', args: { file_path: 'draft/main.md' } },
    },
    {
      type: 'tool_result',
      content: '# manuscript',
      event_id: 'read-1',
      metadata: { tool_name: 'read_file' },
    },
    {
      type: 'tool_call',
      content: 'str_replace',
      event_id: 'edit-1',
      metadata: { tool_name: 'str_replace', args: { file_path: 'draft/main.md' } },
    },
    {
      type: 'tool_result',
      content: 'selection mismatch',
      event_id: 'edit-1',
      metadata: { tool_name: 'str_replace', error: true },
    },
  ]

  it('pairs calls and results into compact steps', () => {
    const steps = buildExecutionSteps(events)

    expect(steps).toHaveLength(2)
    expect(steps[0]).toMatchObject({
      id: 'read-1',
      toolName: 'read_file',
      status: 'success',
    })
    expect(steps[1]).toMatchObject({
      id: 'edit-1',
      toolName: 'str_replace',
      status: 'error',
      result: 'selection mismatch',
    })
  })

  it('summarizes counts without exposing raw tool payloads', () => {
    expect(executionSummary(buildExecutionSteps(events))).toEqual({
      total: 2,
      completed: 1,
      failed: 1,
      denied: 0,
      skipped: 0,
      noChange: 0,
      running: 0,
    })
  })

  it('keeps skipped, denied, and no-change distinct from failures', () => {
    const statusEvents: AgentEvent[] = [
      ['skip', 'skipped'],
      ['deny', 'denied'],
      ['noop', 'no_change'],
    ].flatMap(([id, status]) => [
      {
        type: 'tool_call',
        content: 'str_replace',
        event_id: id,
        metadata: { tool_name: 'str_replace' },
      } as AgentEvent,
      {
        type: 'tool_result',
        content: status,
        event_id: id,
        metadata: { tool_name: 'str_replace', status },
      } as AgentEvent,
    ])
    const steps = buildExecutionSteps(statusEvents)
    expect(steps.map((step) => step.status)).toEqual(['skipped', 'denied', 'no_change'])
    expect(executionSummary(steps)).toEqual({
      total: 3,
      completed: 0,
      failed: 0,
      denied: 1,
      skipped: 1,
      noChange: 1,
      running: 0,
    })
  })

  it('keeps successful execution collapsed and opens failures for attention', async () => {
    const wrapper = mount(AgentExecutionGroup, {
      props: { events: events.slice(0, 2), streaming: false },
    })

    expect(wrapper.find('.execution-group').attributes('open')).toBeUndefined()
    expect(wrapper.find('.execution-group > summary').text()).toContain('已完成 · 1 步')

    await wrapper.setProps({ events })

    expect(wrapper.find('.execution-group').attributes('open')).toBeDefined()
    expect(wrapper.find('.execution-group > summary').text()).toContain('1 步失败')
  })

  it('carries the fuller result_detail for the expanded view', () => {
    const longResult = 'x'.repeat(500)
    const resultEvents: AgentEvent[] = [
      {
        type: 'tool_call',
        content: 'read_file',
        event_id: 'r1',
        metadata: { tool_name: 'read_file' },
      },
      {
        type: 'tool_result',
        content: longResult.slice(0, 200),
        event_id: 'r1',
        metadata: { tool_name: 'read_file', result_detail: longResult },
      },
    ]
    const steps = buildExecutionSteps(resultEvents)
    expect(steps[0].result).toBe(longResult.slice(0, 200))
    expect(steps[0].resultDetail).toBe(longResult)
  })

  it('renders a literature answer as citations and other tools as raw text', () => {
    const answer = JSON.stringify({
      status: 'answered',
      insufficient_reason: null,
      claims: [
        {
          text: '评估使用了留出集。',
          evidence_ids: [`evidence_${'a'.repeat(24)}`],
          evidence_status: 'supported',
        },
      ],
      evidence: [
        {
          evidence_id: `evidence_${'a'.repeat(24)}`,
          source_id: 'src_demo_0001',
          title: 'Demo Paper A',
          page: 2,
          exact_quote: 'Only this page mentions the held-out split.',
          context_before: '',
          context_after: '',
        },
      ],
      rejected_claims: [],
      unresolved_count: 0,
    })
    const literatureEvents: AgentEvent[] = [
      {
        type: 'tool_call',
        content: 'literature_answer',
        event_id: 'lit-1',
        metadata: { tool_name: 'literature_answer', args: { question: 'Q?' } },
      },
      {
        type: 'tool_result',
        content: answer,
        event_id: 'lit-1',
        metadata: { tool_name: 'literature_answer' },
      },
    ]

    const wrapper = mount(AgentExecutionGroup, { props: { events: literatureEvents } })

    expect(wrapper.find('[data-testid="literature-answer"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="literature-claim"]').text()).toContain('评估使用了留出集。')
    // The result is rendered as citations, not as raw JSON.
    expect(wrapper.find('[data-testid="literature-raw"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('"exact_quote"')
  })

  it('only treats unmatched approvals as pending attention', () => {
    const settled: AgentEvent[] = [
      {
        type: 'await_approval',
        content: '',
        event_id: 'ap-1',
        metadata: { tool_name: 'write_file' },
      },
      { type: 'approval_received', content: '', event_id: 'ap-1' },
    ]
    const pending: AgentEvent[] = [
      {
        type: 'await_approval',
        content: '',
        event_id: 'ap-2',
        metadata: { tool_name: 'write_file' },
      },
    ]
    expect(hasPendingApproval(settled)).toBe(false)
    expect(hasPendingApproval(pending)).toBe(true)
    expect(hasPendingApproval([...settled, ...pending])).toBe(true)
  })

  it('recognizes only structured partial outcomes as recoverable', () => {
    expect(
      hasRecoverablePartial([
        {
          type: 'response',
          content: '',
          metadata: { partial: true, stop_code: 'no_progress_stall' },
        },
      ]),
    ).toBe(true)
    expect(
      hasRecoverablePartial([{ type: 'response', content: 'The word partial is ordinary text.' }]),
    ).toBe(false)
  })
})
