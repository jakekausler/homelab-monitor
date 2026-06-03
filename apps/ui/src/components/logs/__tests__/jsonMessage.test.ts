import { describe, it, expect } from 'vitest'
import {
  detectJsonMessage,
  jsonTopLevelKeys,
  buildJsonModel,
  JSON_MAX_CHILDREN,
  type JsonNode,
  type JsonLeafNode,
  type JsonTruncatedNode,
} from '../jsonMessage'

describe('detectJsonMessage', () => {
  it('detects object as tree', () => {
    const result = detectJsonMessage('{"a":1}')
    expect(result).toEqual({ kind: 'tree', value: { a: 1 } })
  })

  it('detects array as tree', () => {
    const result = detectJsonMessage('[1,2]')
    expect(result).toEqual({ kind: 'tree', value: [1, 2] })
  })

  it('detects empty object as tree', () => {
    const result = detectJsonMessage('{}')
    expect(result).toEqual({ kind: 'tree', value: {} })
  })

  it('detects empty array as tree', () => {
    const result = detectJsonMessage('[]')
    expect(result).toEqual({ kind: 'tree', value: [] })
  })

  it('detects number as text', () => {
    const result = detectJsonMessage('42')
    expect(result).toEqual({ kind: 'text' })
  })

  it('detects quoted string as text', () => {
    const result = detectJsonMessage('"hello"')
    expect(result).toEqual({ kind: 'text' })
  })

  it('detects boolean as text', () => {
    const result = detectJsonMessage('true')
    expect(result).toEqual({ kind: 'text' })
  })

  it('detects null as text', () => {
    const result = detectJsonMessage('null')
    expect(result).toEqual({ kind: 'text' })
  })

  it('detects invalid JSON as text', () => {
    const result = detectJsonMessage('{not json')
    expect(result).toEqual({ kind: 'text' })
  })

  it('detects plain text as text', () => {
    const result = detectJsonMessage('Request received')
    expect(result).toEqual({ kind: 'text' })
  })
})

describe('jsonTopLevelKeys', () => {
  it('returns keys for object', () => {
    const result = jsonTopLevelKeys({ a: 1, b: 2 })
    expect(result).toEqual(['a', 'b'])
  })

  it('returns empty array for array', () => {
    const result = jsonTopLevelKeys([1, 2])
    expect(result).toEqual([])
  })

  it('returns empty array for number', () => {
    const result = jsonTopLevelKeys(42)
    expect(result).toEqual([])
  })

  it('returns empty array for string', () => {
    const result = jsonTopLevelKeys('str')
    expect(result).toEqual([])
  })

  it('returns empty array for null', () => {
    const result = jsonTopLevelKeys(null)
    expect(result).toEqual([])
  })

  it('returns empty array for boolean', () => {
    const result = jsonTopLevelKeys(true)
    expect(result).toEqual([])
  })
})

describe('buildJsonModel', () => {
  it('builds simple object', () => {
    const model = buildJsonModel({ a: 1, b: 'x' })
    expect(model.type).toBe('branch')
    if (model.type === 'branch') {
      expect(model.container).toBe('object')
      expect(model.childCount).toBe(2)
      expect(model.hiddenCount).toBe(0)
      expect(model.children).toHaveLength(2)

      const childA = model.children.find((c) => c.path === 'root.a')
      expect(childA?.type).toBe('leaf')
      if (childA?.type === 'leaf') {
        expect(childA.valueKind).toBe('number')
        expect(childA.text).toBe('1')
      }

      const childB = model.children.find((c) => c.path === 'root.b')
      expect(childB?.type).toBe('leaf')
      if (childB?.type === 'leaf') {
        expect(childB.valueKind).toBe('string')
        expect(childB.text).toBe('"x"')
      }
    }
  })

  it('builds array with correct paths and labels', () => {
    const model = buildJsonModel([1, 2])
    expect(model.type).toBe('branch')
    if (model.type === 'branch') {
      expect(model.container).toBe('array')
      expect(model.childCount).toBe(2)
      expect(model.children).toHaveLength(2)
      expect(model.children[0]?.path).toBe('root[0]')
      expect(model.children[0]?.label).toBe('0')
      expect(model.children[1]?.path).toBe('root[1]')
      expect(model.children[1]?.label).toBe('1')
    }
  })

  it('renders all leaf kinds correctly', () => {
    const model = buildJsonModel({ s: 'x', n: 1, b: true, z: null })
    expect(model.type).toBe('branch')
    if (model.type === 'branch') {
      const leaves = model.children.reduce(
        (acc, child) => {
          if (child.type === 'leaf') {
            acc[child.label ?? ''] = child
          }
          return acc
        },
        {} as Record<string, JsonLeafNode>,
      )

      expect(leaves.s?.valueKind).toBe('string')
      expect(leaves.s?.text).toBe('"x"')
      expect(leaves.n?.valueKind).toBe('number')
      expect(leaves.n?.text).toBe('1')
      expect(leaves.b?.valueKind).toBe('boolean')
      expect(leaves.b?.text).toBe('true')
      expect(leaves.z?.valueKind).toBe('null')
      expect(leaves.z?.text).toBe('null')
    }
  })

  it('enforces depth cap', () => {
    let v: unknown = { leaf: 1 }
    for (let i = 0; i < 12; i++) {
      v = { next: v }
    }

    const model = buildJsonModel(v)

    // Walk the tree to find a truncated node
    const findTruncated = (node: JsonNode): JsonTruncatedNode | null => {
      if (node.type === 'truncated') return node
      if (node.type === 'branch') {
        for (const child of node.children) {
          const result = findTruncated(child)
          if (result) return result
        }
      }
      return null
    }

    const truncated = findTruncated(model)
    expect(truncated).not.toBeNull()
    expect(truncated?.type).toBe('truncated')
    expect(truncated?.reason).toBe('depth')
    expect(truncated?.preview).toBeTruthy()
  })

  it('enforces child cap on objects', () => {
    const obj = Object.fromEntries(Array.from({ length: 1001 }, (_, i) => [`k${i}`, i]))
    const model = buildJsonModel(obj)

    expect(model.type).toBe('branch')
    if (model.type === 'branch') {
      expect(model.childCount).toBe(1001)
      expect(model.children).toHaveLength(JSON_MAX_CHILDREN)
      expect(model.hiddenCount).toBe(1)
    }
  })

  it('enforces child cap on arrays', () => {
    const arr = Array.from({ length: 1500 }, (_, i) => i)
    const model = buildJsonModel(arr)

    expect(model.type).toBe('branch')
    if (model.type === 'branch') {
      expect(model.childCount).toBe(1500)
      expect(model.children).toHaveLength(JSON_MAX_CHILDREN)
      expect(model.hiddenCount).toBe(500)
    }
  })

  it('enforces budget cap on large structures', () => {
    // Create structure with many branches: array of 200 objects with 30 keys each
    // ≈ 200 + 200*30 = 6200 nodes, exceeds JSON_MAX_NODES
    const arr = Array.from({ length: 200 }, () =>
      Object.fromEntries(Array.from({ length: 30 }, (_, i) => [`field${i}`, i])),
    )
    const model = buildJsonModel(arr)

    // Walk the tree to find a budget-truncated node
    const findBudgetTruncated = (node: JsonNode): JsonTruncatedNode | null => {
      if (node.type === 'truncated' && node.reason === 'budget') return node
      if (node.type === 'branch') {
        for (const child of node.children) {
          const result = findBudgetTruncated(child)
          if (result) return result
        }
      }
      return null
    }

    const budgetTruncated = findBudgetTruncated(model)
    expect(budgetTruncated).not.toBeNull()
    expect(budgetTruncated?.type).toBe('truncated')
    expect(budgetTruncated?.reason).toBe('budget')
  })
})
