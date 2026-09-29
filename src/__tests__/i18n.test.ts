import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { createAppI18n } from '../i18n'
import zhMessages from '../i18n/locales/zh-CN.json'
import enMessages from '../i18n/locales/en-US.json'

function getAllKeys(obj: Record<string, unknown>, prefix = ''): string[] {
  const keys: string[] = []
  for (const key of Object.keys(obj)) {
    const fullKey = prefix ? `${prefix}.${key}` : key
    if (typeof obj[key] === 'object' && obj[key] !== null) {
      keys.push(...getAllKeys(obj[key] as Record<string, unknown>, fullKey))
    } else {
      keys.push(fullKey)
    }
  }
  return keys.sort()
}

/**
 * `JSON.parse` silently keeps the last value of a duplicate member, so a locale
 * file can carry two different translations for one key and pass every existing
 * check.  This scanner reports duplicate member names inside the same object.
 */
function duplicateKeys(raw: string): string[] {
  const duplicates: string[] = []
  const stack: Array<{ keys: Set<string>; path: string }> = []
  let lastKey = ''
  let index = 0

  while (index < raw.length) {
    const char = raw[index]
    if (char === '"') {
      let end = index + 1
      while (end < raw.length && !(raw[end] === '"' && raw[end - 1] !== '\\')) end += 1
      const text = raw.slice(index + 1, end)
      let probe = end + 1
      while (probe < raw.length && /\s/.test(raw[probe])) probe += 1
      if (raw[probe] === ':') {
        const top = stack[stack.length - 1]
        if (top) {
          if (top.keys.has(text)) duplicates.push(top.path ? `${top.path}.${text}` : text)
          top.keys.add(text)
        }
        lastKey = text
      }
      index = end + 1
      continue
    }
    if (char === '{') {
      const parent = stack[stack.length - 1]
      const path = parent ? (parent.path ? `${parent.path}.${lastKey}` : lastKey) : ''
      stack.push({ keys: new Set(), path })
    } else if (char === '}') {
      stack.pop()
    }
    index += 1
  }
  return duplicates
}

function readLocale(name: string): string {
  return readFileSync(resolve(process.cwd(), 'src/i18n/locales', name), 'utf-8')
}

describe('i18n setup', () => {
  it('creates i18n instance with zh-CN as default locale', () => {
    const i18n = createAppI18n()
    expect(i18n.global.locale.value).toBe('zh-CN')
  })

  it('creates a vue-i18n instance', () => {
    const i18n = createAppI18n()
    // vue-i18n createI18n returns an object with .global
    expect(i18n.global).toBeDefined()
    expect(typeof i18n.global.t).toBe('function')
  })

  it('has zh-CN and en-US messages loaded', () => {
    const i18n = createAppI18n()
    expect(i18n.global.getLocaleMessage('zh-CN')).toBeDefined()
    expect(i18n.global.getLocaleMessage('en-US')).toBeDefined()
  })

  it('all zh-CN keys have corresponding en-US keys', () => {
    const zhKeys = getAllKeys(zhMessages)
    const enKeys = getAllKeys(enMessages)
    expect(enKeys).toEqual(zhKeys)
  })

  it('can translate a known key in both locales', () => {
    const i18n = createAppI18n()
    i18n.global.locale.value = 'zh-CN'
    expect(typeof i18n.global.t('mode.translate')).toBe('string')
    i18n.global.locale.value = 'en-US'
    expect(typeof i18n.global.t('mode.translate')).toBe('string')
  })

  it('declares no duplicate key inside the same object', () => {
    expect(duplicateKeys(readLocale('zh-CN.json'))).toEqual([])
    expect(duplicateKeys(readLocale('en-US.json'))).toEqual([])
  })

  it('detects a duplicate key when one is introduced', () => {
    // Guards the guard: the scanner must fail on a deliberately duplicated key.
    const broken = '{\n  "taskAgent": {\n    "you": "你",\n    "you": "你",\n  }\n}'
    expect(duplicateKeys(broken)).toEqual(['taskAgent.you'])
  })
})
