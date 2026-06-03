import { describe, expect, it } from 'vitest'

import { msgFilterClause, translateSearchToLogsQl } from '../logsQlTranslate'

describe('translateSearchToLogsQl', () => {
  it('empty string → match-all', () => {
    expect(translateSearchToLogsQl('')).toBe('*')
  })

  it('whitespace-only → match-all', () => {
    expect(translateSearchToLogsQl('   ')).toBe('*')
  })

  it('plain phrase → quoted _msg filter', () => {
    expect(translateSearchToLogsQl('connection refused')).toBe('_msg:"connection refused"')
  })

  it('trims surrounding whitespace before quoting', () => {
    expect(translateSearchToLogsQl('  padded  ')).toBe('_msg:"padded"')
  })

  it('escapes an embedded double-quote', () => {
    expect(translateSearchToLogsQl('say "hi"')).toBe('_msg:"say \\"hi\\""')
  })

  it('escapes an embedded backslash', () => {
    expect(translateSearchToLogsQl('a\\b')).toBe('_msg:"a\\\\b"')
  })

  it('escapes backslash FIRST then quote when both present', () => {
    // Input:  \"   (one backslash, one quote)
    // Expect: _msg:"\\\""  →  backslash becomes \\, quote becomes \"
    expect(translateSearchToLogsQl('\\"')).toBe('_msg:"\\\\\\""')
  })
})

describe('msgFilterClause', () => {
  it('empty string → null', () => {
    expect(msgFilterClause('')).toBeNull()
  })

  it('whitespace-only → null', () => {
    expect(msgFilterClause('   ')).toBeNull()
  })

  it('plain value → _msg clause', () => {
    expect(msgFilterClause('host-1')).toBe('_msg:"host-1"')
  })

  it('escapes embedded double-quote', () => {
    expect(msgFilterClause('say "hi"')).toBe('_msg:"say \\"hi\\""')
  })

  it('escapes embedded backslash', () => {
    expect(msgFilterClause('a\\b')).toBe('_msg:"a\\\\b"')
  })
})
