import eslint from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

const directive = /^\s*(eslint|global\b|@ts-|prettier-ignore|#region|#endregion)|^!/;

const noComments = {
  meta: {
    type: "suggestion",
    messages: {
      comment:
        "No new comments. Make the code say it, or put product rules in docs/domain.md. After deleting comments run `npm run lint -- --prune-suppressions`.",
    },
    schema: [],
  },
  create(context) {
    return {
      Program() {
        for (const comment of context.sourceCode.getAllComments()) {
          if (comment.type === "Shebang" || directive.test(comment.value)) continue;
          context.report({ loc: comment.loc, messageId: "comment" });
        }
      },
    };
  },
};

export default tseslint.config(
  {
    ignores: ["build/**", "node_modules/**", "playwright-report/**", "test-results/**"],
  },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx,js,mjs}"],
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    plugins: { clashlens: { rules: { "no-comments": noComments } } },
    rules: {
      "clashlens/no-comments": "error",
      "no-empty": ["error", { allowEmptyCatch: true }],
    },
  },
);
