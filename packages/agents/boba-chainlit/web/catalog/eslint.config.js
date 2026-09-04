import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default defineConfig(
  globalIgnores(["dist", "node_modules", "eslint.config.js", "vite.config.ts", "src/api/schema.d.ts"]),
  js.configs.recommended,
  tseslint.configs.strictTypeChecked,
  tseslint.configs.stylisticTypeChecked,
  {
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "@typescript-eslint/consistent-type-definitions": ["error", "type"],
      "@typescript-eslint/no-non-null-assertion": "error",
      "@typescript-eslint/restrict-template-expressions": ["error", { allowNumber: true }],
    },
  },
  {
    // классы виджетов существуют только внутри src/ui: снаружи — компонент из ui/
    files: ["src/**/*.tsx"],
    ignores: ["src/ui/**"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector:
            'JSXAttribute[name.name="className"] Literal[value=/^(btn|chip|code|dialog|empty|eyebrow|facts|field|icon-btn|index|input|list|note|page|panel|row|search|section|segmented|stack|table|toast|toolbar|topbar|alert)([ _-].*)?$/]',
          message: "widget classes live in src/ui: use the widget component instead",
        },
      ],
    },
  },
);
