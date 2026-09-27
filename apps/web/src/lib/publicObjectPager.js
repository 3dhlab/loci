export const PUBLIC_OBJECT_PAGE_SIZE = 3
export const PUBLIC_OBJECT_PAGE_PARAM = 'object_page'

export function readPublicObjectPage(search = '') {
  const value = Number(new URLSearchParams(search).get(PUBLIC_OBJECT_PAGE_PARAM))
  return Number.isInteger(value) && value > 0 ? value : 1
}

export function buildPublicBrowsePageUrl({ search = '', page = 1, projectId } = {}) {
  const params = new URLSearchParams(search)
  const normalizedProjectId = String(projectId ?? params.get('project_id') ?? params.get('project') ?? '').trim()
  params.delete('project')
  params.delete('project_id')
  params.delete(PUBLIC_OBJECT_PAGE_PARAM)
  if (normalizedProjectId) params.set('project_id', normalizedProjectId)
  if (page > 1) params.set(PUBLIC_OBJECT_PAGE_PARAM, String(page))
  const serialized = params.toString()
  return `/public${serialized ? `?${serialized}` : ''}`
}

export function normalizePublicObjectPageResponse(response) {
  const items = Array.isArray(response?.items) ? response.items : []
  const page = Math.max(1, Number(response?.page) || 1)
  const pageSize = Math.max(1, Number(response?.page_size) || PUBLIC_OBJECT_PAGE_SIZE)
  const total = Math.max(0, Number(response?.total) || 0)
  const totalPages = Math.max(0, Number(response?.total_pages) || Math.ceil(total / pageSize))
  return { items, page, pageSize, total, totalPages }
}

export function getPublicObjectPageButtons(totalPages, currentPage) {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, index) => index + 1)
  const visible = new Set([1, totalPages, currentPage - 1, currentPage, currentPage + 1])
  const pages = [...visible].filter((page) => page >= 1 && page <= totalPages).sort((a, b) => a - b)
  return pages.flatMap((page, index) => {
    if (index === 0) return [page]
    const previous = pages[index - 1]
    return [...(page - previous > 1 ? ['ellipsis'] : []), page]
  })
}
