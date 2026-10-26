import assert from 'node:assert/strict'
import { mkdtemp, realpath, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { createServer } from 'vite'

test('Vite denies synthetic restricted files with raw and inline queries', async (t) => {
  const root = await realpath(await mkdtemp(join(tmpdir(), 'loci-vite-deny-')))
  const marker = 'LOCI_SYNTHETIC_DENIED_FILE_NO_PRIVATE_DATA'
  let server
  t.after(async () => {
    try {
      await server?.close()
    } finally {
      await rm(root, { recursive: true, force: true })
    }
  })
  await writeFile(join(root, 'denied-fixture.crt'), marker)
  await writeFile(join(root, 'allowed.txt'), 'synthetic public control')
  server = await createServer({
    root,
    configFile: false,
    envFile: false,
    logLevel: 'silent',
    optimizeDeps: { noDiscovery: true },
    server: {
      host: '127.0.0.1',
      port: 0,
      fs: { strict: true, allow: [root], deny: ['**/denied-fixture.crt'] },
    },
  })
  await server.listen()
  const address = server.httpServer.address()
  assert.equal(address.address, '127.0.0.1')
  const origin = `http://127.0.0.1:${address.port}`
  const control = await fetch(`${origin}/allowed.txt`)
  assert.equal(control.status, 200)
  assert.equal(await control.text(), 'synthetic public control')
  for (const query of ['', '?raw', '?import&raw', '?import&url&inline']) {
    await t.test(query || 'plain request', async (subtest) => {
      const response = await fetch(`${origin}/denied-fixture.crt${query}`)
      const body = await response.text()
      subtest.diagnostic(`status=${response.status}; synthetic marker returned=${body.includes(marker) || body.includes(Buffer.from(marker).toString('base64'))}`)
      assert.equal(response.status, 403)
      assert.ok(!body.includes(marker))
      assert.ok(!body.includes(Buffer.from(marker).toString('base64')))
    })
  }
})
