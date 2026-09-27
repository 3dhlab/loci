export function semanticFitTier(score) {
  if (!Number.isFinite(score)) return null
  const normalized = Math.min(1, Math.max(0, score))
  if (normalized >= 0.78) return { label: 'Close match', tone: 'strong' }
  if (normalized >= 0.62) return { label: 'Related match', tone: 'medium' }
  return { label: 'Broad match', tone: 'loose' }
}
