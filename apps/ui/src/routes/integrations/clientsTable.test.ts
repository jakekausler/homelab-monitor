import { describe, expect, it } from 'vitest'

import {
  clientConnection,
  clientDisplayName,
  filterClients,
  sortClients,
  type ClientRow,
} from './clientsTable'

// Fixture builder
function row(over: Partial<ClientRow>): ClientRow {
  return {
    ap_mac: null,
    hostname: null,
    ip: null,
    is_host: false,
    last_seen: '2026-06-20T00:00:00Z',
    lease_expiry: null,
    mac: 'aa:aa:aa:aa:aa:aa',
    name: null,
    network: 'LAN',
    online: true,
    use_fixedip: false,
    ...over,
  }
}

describe('clientsTable', () => {
  describe('clientDisplayName', () => {
    it('prefers name when present', () => {
      expect(clientDisplayName(row({ name: 'Box', hostname: 'box.local', mac: 'aa:bb:cc' }))).toBe(
        'Box',
      )
    })

    it('falls back to hostname when name is null', () => {
      expect(clientDisplayName(row({ name: null, hostname: 'box.local', mac: 'aa:bb:cc' }))).toBe(
        'box.local',
      )
    })

    it('falls back to mac when name and hostname are null', () => {
      expect(clientDisplayName(row({ name: null, hostname: null, mac: 'aa:bb:cc' }))).toBe(
        'aa:bb:cc',
      )
    })
  })

  describe('clientConnection', () => {
    it('shows Wi-Fi with AP MAC when ap_mac is present', () => {
      expect(clientConnection(row({ ap_mac: 'ap:11:22:33:44:55' }))).toBe(
        'Wi-Fi (ap:11:22:33:44:55)',
      )
    })

    it('shows Wired when ap_mac is null', () => {
      expect(clientConnection(row({ ap_mac: null }))).toBe('Wired')
    })
  })

  describe('filterClients', () => {
    const clients = [
      row({ name: 'Box', hostname: 'box.local', mac: 'aa:bb:cc', ip: '192.168.1.5' }),
      row({ name: 'Phone', hostname: null, mac: 'dd:ee:ff', ip: '192.168.1.10' }),
      row({ name: null, hostname: 'nas.local', mac: 'gg:hh:ii', ip: '192.168.1.20' }),
    ]

    it('returns all rows when query is empty', () => {
      expect(filterClients(clients, '')).toEqual(clients)
    })

    it('returns all rows when query is whitespace', () => {
      expect(filterClients(clients, '   ')).toEqual(clients)
    })

    it('matches on name case-insensitive', () => {
      expect(filterClients(clients, 'box')).toHaveLength(1)
      expect(filterClients(clients, 'BOX')).toHaveLength(1)
      expect(filterClients(clients, 'phone')).toHaveLength(1)
    })

    it('matches on hostname case-insensitive', () => {
      expect(filterClients(clients, 'box.local')).toHaveLength(1)
      expect(filterClients(clients, 'nas.local')).toHaveLength(1)
    })

    it('matches on ip', () => {
      expect(filterClients(clients, '192.168.1.5')).toHaveLength(1)
      expect(filterClients(clients, '192.168.1.10')).toHaveLength(1)
    })

    it('matches on mac substring', () => {
      expect(filterClients(clients, 'aa:bb')).toHaveLength(1)
      expect(filterClients(clients, 'dd:ee')).toHaveLength(1)
    })

    it('returns empty when no match', () => {
      expect(filterClients(clients, 'nomatch')).toHaveLength(0)
    })
  })

  describe('sortClients', () => {
    const clients = [
      row({
        name: 'Zebra',
        ip: '192.168.1.30',
        mac: 'zz:zz:zz',
        last_seen: '2026-06-21T00:00:00Z',
        online: false,
      }),
      row({
        name: 'Apple',
        ip: '192.168.1.10',
        mac: 'aa:aa:aa',
        last_seen: '2026-06-20T00:00:00Z',
        online: true,
      }),
      row({
        name: 'Box',
        ip: '192.168.1.20',
        mac: 'bb:bb:bb',
        last_seen: '2026-06-20T12:00:00Z',
        online: true,
      }),
    ]

    it('sorts by name asc', () => {
      const sorted = sortClients(clients, 'name', 'asc')
      expect(sorted[0]?.name).toBe('Apple')
      expect(sorted[1]?.name).toBe('Box')
      expect(sorted[2]?.name).toBe('Zebra')
    })

    it('sorts by name desc', () => {
      const sorted = sortClients(clients, 'name', 'desc')
      expect(sorted[0]?.name).toBe('Zebra')
      expect(sorted[1]?.name).toBe('Box')
      expect(sorted[2]?.name).toBe('Apple')
    })

    it('sorts by ip asc', () => {
      const sorted = sortClients(clients, 'ip', 'asc')
      expect(sorted[0]?.ip).toBe('192.168.1.10')
      expect(sorted[1]?.ip).toBe('192.168.1.20')
      expect(sorted[2]?.ip).toBe('192.168.1.30')
    })

    it('sorts by mac asc', () => {
      const sorted = sortClients(clients, 'mac', 'asc')
      expect(sorted[0]?.mac).toBe('aa:aa:aa')
      expect(sorted[1]?.mac).toBe('bb:bb:bb')
      expect(sorted[2]?.mac).toBe('zz:zz:zz')
    })

    it('sorts by last_seen', () => {
      const sorted = sortClients(clients, 'last_seen', 'asc')
      expect(sorted[0]?.last_seen).toBe('2026-06-20T00:00:00Z')
      expect(sorted[1]?.last_seen).toBe('2026-06-20T12:00:00Z')
    })

    it('sorts by online asc (false before true)', () => {
      const sorted = sortClients(clients, 'online', 'asc')
      expect(sorted[0]?.online).toBe(false)
      expect(sorted[1]?.online).toBe(true)
      expect(sorted[2]?.online).toBe(true)
    })

    it('does not mutate input array', () => {
      const original = clients.map((c) => c.mac)
      sortClients(clients, 'name', 'asc')
      const after = clients.map((c) => c.mac)
      expect(original).toEqual(after)
    })
  })
})
