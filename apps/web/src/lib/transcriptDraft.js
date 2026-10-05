// Keep local edits during navigation without retaining stale, clean server text.
export function reconcileTranscriptDraft({ source, draftText, videoId, token, transcript }) {
  const nextSource = {
    videoId,
    token,
    transcriptId: transcript?.id ?? null,
    rawText: transcript?.raw_text ?? ''
  }
  const sameViewer = source?.videoId === videoId && source?.token === token
  const clean = sameViewer && draftText === source.rawText
  if (!sameViewer || clean || (transcript && draftText === nextSource.rawText)) {
    return { action: 'replace', source: nextSource, text: nextSource.rawText }
  }
  const serverChanged = source.transcriptId !== nextSource.transcriptId || source.rawText !== nextSource.rawText
  return { action: serverChanged ? 'conflict' : 'preserve', source, text: draftText }
}
