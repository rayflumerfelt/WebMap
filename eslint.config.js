// Flat config (ESLint 9). `07-frontend.md` §1.1 sketches these rules as
// `.eslintrc.cjs`; the element types and the allow-matrix below are that
// sketch verbatim, ported to the config format ESLint 9 actually loads.
import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import boundaries from 'eslint-plugin-boundaries';
import reactHooks from 'eslint-plugin-react-hooks';

/**
 * Elements are matched in `full` mode so that every file under a package is
 * classified, not just the ones sitting in a captured subfolder. Order
 * matters: the first pattern that matches wins, so `apps/web` must not be
 * shadowed by a broader `apps/*`.
 */
const ELEMENTS = [
  { type: 'model', pattern: 'packages/style-model/**/*', mode: 'full' },
  { type: 'ui', pattern: 'packages/ui/**/*', mode: 'full' },
  { type: 'map', pattern: 'packages/map/**/*', mode: 'full' },
  { type: 'app', pattern: 'apps/**/*', mode: 'full' },
];

export default tseslint.config(
  {
    // The Python virtualenv ships JavaScript fixtures (win32com test
    // scriptlets, packaged web assets). Linting them produces thousands of
    // errors about globals that do not exist in any environment we target.
    ignores: [
      '**/.venv/**',
      '**/node_modules/**',
      '**/dist/**',
      '**/dist-types/**',
      '**/.turbo/**',
      '**/*.d.ts',
      'tests/visual/output/**',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    // React's hook rules. `07-frontend.md` §2.2 names the recreate-the-map bug
    // as the commonest one in this area, and `exhaustive-deps` is what catches
    // its relatives — a stale closure in an effect, a listener re-registered
    // on every render. The one deliberate suppression in the codebase (the
    // create-once effect in WebMap.tsx) carries a comment saying why.
    files: ['apps/**/*.{ts,tsx}', 'packages/**/*.{ts,tsx}'],
    plugins: { boundaries, 'react-hooks': reactHooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
    },
    languageOptions: {
      globals: {
        // The browser surface this application actually uses. Declared
        // explicitly rather than pulled from a preset, so a reference to
        // something exotic is a lint error and a deliberate decision.
        window: 'readonly',
        document: 'readonly',
        navigator: 'readonly',
        console: 'readonly',
        fetch: 'readonly',
        setTimeout: 'readonly',
        clearTimeout: 'readonly',
        requestAnimationFrame: 'readonly',
        HTMLDivElement: 'readonly',
        HTMLElement: 'readonly',
        Blob: 'readonly',
        BroadcastChannel: 'readonly',
      },
    },
    settings: {
      'boundaries/include': ['apps/**/*', 'packages/**/*'],
      'boundaries/elements': ELEMENTS,
      'import/resolver': {
        typescript: { alwaysTryTypes: true, project: ['packages/*/tsconfig.json', 'apps/*/tsconfig.json'] },
      },
    },
    rules: {
      // TypeScript resolves identifiers itself, and far better than this rule
      // can. Leaving it on means maintaining a globals list twice.
      'no-undef': 'off',
      // CLAUDE.md §5: no `any`, `unknown` plus narrowing.
      '@typescript-eslint/no-explicit-any': 'error',
      // CLAUDE.md §5: no default exports except React components required to
      // have them. Those files opt out with an explanatory eslint-disable.
      'no-restricted-syntax': [
        'error',
        {
          selector: 'ExportDefaultDeclaration',
          message:
            'No default exports (CLAUDE.md §5). Use a named export so renames are ' +
            'mechanical and the symbol is greppable.',
        },
      ],
      'boundaries/element-types': [
        'error',
        {
          default: 'disallow',
          message: '${file.type} may not import ${dependency.type} — see CLAUDE.md §3.5.',
          rules: [
            { from: ['map'], allow: ['ui', 'model', 'map'] },
            { from: ['ui'], allow: ['model', 'ui'] },
            { from: ['model'], allow: ['model'] },
            { from: ['app'], allow: ['map', 'ui', 'model', 'app'] },
          ],
        },
      ],
      // The package-boundary rules that are about *dependencies* rather than
      // about our own modules. CLAUDE.md §3.5.
      'boundaries/external': [
        'error',
        {
          default: 'allow',
          rules: [
            {
              from: ['ui'],
              disallow: ['maplibre-gl'],
              message:
                '@webmap/ui must not import MapLibre (CLAUDE.md §3.5). The legend and ' +
                'ramp editor render in the headless render shell, which has no map ' +
                'instance in scope. @maplibre/maplibre-gl-style-spec is fine — it is ' +
                'data, not a renderer.',
            },
            {
              from: ['model'],
              disallow: ['maplibre-gl', 'react', 'react-dom'],
              message:
                '@webmap/style-model must not import React or MapLibre (CLAUDE.md §3.5). ' +
                'It is pure data transformation, shared with tooling and testable in Node.',
            },
          ],
        },
      ],
    },
  },
  {
    // Config and test files are tooling, not product code.
    files: [
      '**/*.config.{ts,js}',
      '**/*.test.{ts,tsx}',
      '**/vitest.setup.ts',
      'eslint.config.js',
    ],
    rules: { 'no-restricted-syntax': 'off', 'boundaries/element-types': 'off' },
  },
);
