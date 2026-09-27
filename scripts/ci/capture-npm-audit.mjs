import { spawnSync } from 'node:child_process'
import { writeFileSync } from 'node:fs'

const outputPath = process.env.CI_AUDIT_OUTPUT || '../../dependency-audit-full.json'
const runAudit = args => {
  const result = spawnSync('npm', ['audit', '--json', ...args], { encoding: 'utf8' })
  let report
  try { report = JSON.parse(result.stdout) } catch {
    throw new Error(`npm audit failed to return JSON (exit ${result.status}): ${result.stderr || result.stdout}`)
  }
  if (report.error || !report.metadata?.vulnerabilities) {
    throw new Error(`npm audit report is incomplete: ${JSON.stringify(report.error || report)}`)
  }
  return { result, report }
}

const full = runAudit([])
writeFileSync(outputPath, `${JSON.stringify(full.report, null, 2)}\n`)
const production = runAudit(['--omit=dev'])
const prodCounts = production.report.metadata.vulnerabilities
const fullCounts = full.report.metadata.vulnerabilities
const summary = `### npm audit\n\n- Candidate: \`${process.env.GITHUB_SHA || 'local-working-tree'}\`\n- Production dependencies: ${JSON.stringify(prodCounts)}\n- Full tree (including build tools): ${JSON.stringify(fullCounts)}\n- Full report: \`${outputPath}\`\n`
if (process.env.GITHUB_STEP_SUMMARY) {
  const { appendFileSync } = await import('node:fs')
  appendFileSync(process.env.GITHUB_STEP_SUMMARY, summary)
} else process.stdout.write(summary)
if (Object.values(fullCounts).some(count => count > 0)) {
  throw new Error('Dependency advisories remain in the locked tree; review the full audit artifact and update or explicitly disposition each finding.')
}
