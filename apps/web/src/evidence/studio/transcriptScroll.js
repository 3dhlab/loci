export function calculateTranscriptScrollTop({ listTop, rowTop, scrollTop, listHeight, rowHeight, scrollHeight }) {
  const finiteOrZero = (value) => (Number.isFinite(value) ? value : 0)
  const safeListTop = finiteOrZero(listTop)
  const safeRowTop = finiteOrZero(rowTop)
  const safeScrollTop = finiteOrZero(scrollTop)
  const safeListHeight = Math.max(0, finiteOrZero(listHeight))
  const safeRowHeight = Math.max(0, finiteOrZero(rowHeight))
  const safeScrollHeight = Math.max(0, finiteOrZero(scrollHeight))
  const rowTopInScrollContent = safeScrollTop + safeRowTop - safeListTop
  const centeredTop = rowTopInScrollContent - Math.max(0, (safeListHeight - safeRowHeight) / 2)
  return Math.min(Math.max(0, centeredTop), Math.max(0, safeScrollHeight - safeListHeight))
}
