// Draft coordinates stay blank until a surface click or an existing pin loads.
// Zero is a valid coordinate, including at a model's origin.
export function hasAnnotationPoint(draft) {
  return ['point_x', 'point_y', 'point_z'].every(key => {
    const value = draft[key]
    return (typeof value === 'string' || typeof value === 'number')
      && String(value).trim() !== '' && Number.isFinite(Number(value))
  })
}
