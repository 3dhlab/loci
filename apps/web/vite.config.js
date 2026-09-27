import { fileURLToPath, URL } from 'node:url'
import { resolve } from 'node:path'

import { sentryVitePlugin } from '@sentry/vite-plugin'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

const ROOT = fileURLToPath(new URL('.', import.meta.url))

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const release = String(env.VITE_APP_RELEASE || '').trim()
  const sentryConfigReady = Boolean(env.SENTRY_AUTH_TOKEN && env.SENTRY_ORG && env.SENTRY_PROJECT)
  const allowLanDevServer = ['1', 'true', 'enabled'].includes(
    String(env.LOCI_ALLOW_LAN_DEV_SERVER || '').trim().toLowerCase()
  )
  const developmentHost = allowLanDevServer ? '0.0.0.0' : '127.0.0.1'
  const plugins = [react()]

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
