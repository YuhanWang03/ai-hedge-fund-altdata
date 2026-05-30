import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular'],
      },
      colors: {
        // Subtle dark palette
        ink: {
          50:  '#f7f8fa',
          100: '#eef0f4',
          200: '#d9dde6',
          300: '#b3bbcc',
          400: '#7a8499',
          500: '#4d5670',
          600: '#323a52',
          700: '#222843',
          800: '#161b30',
          900: '#0c1022',
        },
        accent: {
          // semantic palette for event types
          fd:     '#3b82f6',  // blue
          llm:    '#a855f7',  // purple
          tavily: '#10b981',  // emerald
          db:     '#f59e0b',  // amber
          intent: '#ef4444',  // red
          module: '#6366f1',  // indigo
        },
      },
    },
  },
  plugins: [],
} satisfies Config
