import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath, URL } from 'node:url'
import { resolve } from 'node:path'

import { sentryVitePlugin } from '@sentry/vite-plugin'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

const ROOT = fileURLToPath(new URL('.', import.meta.url))

function publicSitemapPlugin(siteUrl) {
  let siteOrigin

  return {
    name: 'loci-public-sitemap',
    configResolved() {
      const configuredUrl = String(siteUrl || 'http://localhost:8080').trim()
      let parsedUrl
      try {
        parsedUrl = new URL(configuredUrl)
      } catch {
        throw new Error('VITE_PUBLIC_SITE_URL must be an absolute public site URL.')
      }
      if (!['http:', 'https:'].includes(parsedUrl.protocol) || parsedUrl.username || parsedUrl.password || (parsedUrl.pathname !== '/' && parsedUrl.pathname !== '')) {
        throw new Error('VITE_PUBLIC_SITE_URL must contain only the public site origin, such as https://example.org.')
      }
      siteOrigin = parsedUrl.origin
    },
    async writeBundle(outputOptions) {
      const outputDirectory = resolve(outputOptions.dir || resolve(ROOT, 'dist'))
      await mkdir(outputDirectory, { recursive: true })
      const publicPaths = ['/', '/public']
      const locations = publicPaths.map((path) => `  <url><loc>${siteOrigin}${path}</loc></url>`).join('\n')
      const sitemap = `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${locations}\n</urlset>\n`
      await writeFile(resolve(outputDirectory, 'sitemap.xml'), sitemap)

      const robotsPath = resolve(outputDirectory, 'robots.txt')
      const robots = await readFile(robotsPath, 'utf8').catch(() => 'User-agent: *\n')
      const robotsWithoutSitemap = robots.split(/\r?\n/).filter((line) => !/^\s*sitemap:/i.test(line))
      robotsWithoutSitemap.push(`Sitemap: ${siteOrigin}/sitemap.xml`)
      await writeFile(robotsPath, `${robotsWithoutSitemap.join('\n').replace(/\n+$/, '')}\n`)
    },
  }
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const release = String(env.VITE_APP_RELEASE || '').trim()
  const sentryConfigReady = Boolean(env.SENTRY_AUTH_TOKEN && env.SENTRY_ORG && env.SENTRY_PROJECT)
  const allowLanDevServer = ['1', 'true', 'enabled'].includes(
    String(env.LOCI_ALLOW_LAN_DEV_SERVER || '').trim().toLowerCase()
  )
  const developmentHost = allowLanDevServer ? '0.0.0.0' : '127.0.0.1'
  const plugins = [react(), publicSitemapPlugin(env.VITE_PUBLIC_SITE_URL)]

  if (sentryConfigReady) {
    plugins.push(
      ...sentryVitePlugin({
        org: env.SENTRY_ORG,
        project: env.SENTRY_PROJECT,
        authToken: env.SENTRY_AUTH_TOKEN,
        release: release ? { name: release } : undefined,
        sourcemaps: {
          assets: './dist/assets/**',
          ignore: ['**/*.css.map'],
          filesToDeleteAfterUpload: ['./dist/**/*.map'],
        },
        telemetry: false,
        errorHandler: (error) => {
          throw error
        },
      })
    )
  }

  return {
    build: {
      sourcemap: sentryConfigReady,
      rollupOptions: {
        input: {
          // Console + public-browse + evidence + embed bundle (existing).
          main: resolve(ROOT, 'index.html'),
          // Optional editorial landing entry point.
          landing: resolve(ROOT, 'landing.html'),
        },
      },
    },
    plugins,
    server: {
      host: developmentHost,
      port: 5173,
    },
    preview: {
      host: developmentHost,
    },
  }
})
