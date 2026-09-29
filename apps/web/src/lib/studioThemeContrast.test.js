import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const css = await readFile(new URL('../styles.css', import.meta.url), 'utf8')

function rule(selector) {
  const start = css.indexOf(`${selector} {`)
  assert.notEqual(start, -1, `missing CSS rule: ${selector}`)
  const end = css.indexOf('}', start)
  return css.slice(start, end)
}

function tokenFrom(selector, token) {
  const match = rule(selector).match(new RegExp(`${token}:\\s*(#[0-9a-f]{6})`, 'i'))
  assert.ok(match, `missing ${token} in ${selector}`)
  return match[1]
}

function colorFrom(selector, property = 'color') {
  const match = rule(selector).match(new RegExp(`${property}:\\s*var\\(--studio-now-text\\)`))
  assert.ok(match, `${selector} must render text with --studio-now-text`)
}

function rgb(hex) {
  return [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16))
}

function mix(foreground, background, amount) {
  const fg = rgb(foreground)
  const bg = rgb(background)
  return `#${fg.map((channel, i) => Math.round(channel * amount + bg[i] * (1 - amount)).toString(16).padStart(2, '0')).join('')}`
}

function luminance(hex) {
  const channels = rgb(hex).map((channel) => channel / 255).map((value) => (
    value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
  ))
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
}

function contrastRatio(first, second) {
  const values = [luminance(first), luminance(second)].sort((a, b) => b - a)
  return (values[0] + 0.05) / (values[1] + 0.05)
}

test('muted-light Studio accent text meets 4.5:1 on rendered backgrounds', () => {
  const surface = '#fbfcfa'
  const accent = '#bd684b'
  const text = tokenFrom(".studio-shell[data-studio-theme='muted-light']", '--studio-now-text')
  const surfaceText = tokenFrom(".studio-surface[data-studio-theme='muted-light']", '--studio-now-text')
  assert.equal(text, surfaceText)
  assert.equal(tokenFrom(".studio-shell[data-studio-theme='muted-light']", '--studio-now'), accent)

  colorFrom('.studio-transport-time-current')
  colorFrom('.studio-rail-item.is-playing .studio-rail-item-time')
  colorFrom('.studio-transcript-line.is-active .studio-transcript-time')
  colorFrom('.studio-landing-tagline')
  colorFrom('.studio-landing .notify-capture__status--error')

  const activeRow = mix(accent, surface, 0.26)
  assert.equal(rule('.studio-transcript-line.is-active').includes('color-mix(in srgb, var(--studio-now) 26%, var(--studio-surface))'), true)
  assert.ok(contrastRatio(text, surface) >= 4.5, `contrast on surface was ${contrastRatio(text, surface).toFixed(2)}:1`)
  assert.ok(contrastRatio(text, activeRow) >= 4.5, `contrast on active row was ${contrastRatio(text, activeRow).toFixed(2)}:1`)
})
