# Public surface theme configuration

Public Studio surfaces use CSS custom properties so adopters can set their own type and colors without changing component markup. Add overrides after the application stylesheet in the host theme layer.

Font families are configured on `:root`:

```css
:root {
  --font-ui: "Your Sans", ui-sans-serif, sans-serif;
  --font-display: "Your Display", Georgia, serif;
  --font-mono: ui-monospace, monospace;
}
```

Set palette values with the matching surface selector. Public surfaces use `.studio-surface[data-studio-theme="…"]`; the evidence reader uses `.studio-shell[data-studio-theme="…"]`.

```css
.studio-surface[data-studio-theme="muted-light"] {
  --studio-bg: #f4f5f3;
  --studio-surface-color: #fbfcfa;
  --studio-ink: #202a31;
  --studio-ink-soft: #5c686d;
  --studio-line: #d5dcda;
  --studio-now: #bd684b;
  --studio-moment: #547a78;
}
```

The built-in palettes are `cobalt` (light), `muted-light`, and `darkroom`. Save `muted-light` as the value of `loci.studio.theme` to select it. The blocking startup bootstrap reads that value before React paints, and the loading surface uses the same palette token selector as the finished page. Override the corresponding selector values to adapt each palette to a site's brand.
