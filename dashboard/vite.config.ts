// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 43127,
    proxy: {
      '/api': 'http://127.0.0.1:43126',
    },
  },
})
