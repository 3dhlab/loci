import test from 'node:test'
import assert from 'node:assert/strict'
import { buildPublicBrowsePageUrl, getPublicObjectPageButtons, normalizePublicObjectPageResponse, readPublicObjectPage } from './publicObjectPager.js'

test('public object page links preserve search state and use a distinct page parameter', () => {
  assert.equal(readPublicObjectPage('?q=spoon&object_page=2&page=4'), 2)
  assert.equal(readPublicObjectPage('?object_page=0'), 1)
  assert.equal(buildPublicBrowsePageUrl({ search: '?q=spoon&page=4&project_id=abc', page: 2 }), '/public?q=spoon&page=4&project_id=abc&object_page=2')
  assert.equal(buildPublicBrowsePageUrl({ search: '?q=spoon&project_id=abc&object_page=2', page: 1, projectId: 'def' }), '/public?q=spoon&project_id=def')
})

test('normalizes paged API metadata without inferring totals from the visible items', () => {
  assert.deepEqual(normalizePublicObjectPageResponse({ items: [{ id: 1 }], page: 2, page_size: 3, total: 6, total_pages: 2 }), {
    items: [{ id: 1 }], page: 2, pageSize: 3, total: 6, totalPages: 2,
  })
})

test('keeps page controls compact when the catalog grows', () => {
  assert.deepEqual(getPublicObjectPageButtons(20, 10), [1, 'ellipsis', 9, 10, 11, 'ellipsis', 20])
})
