import eslint from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

const directive = /^\s*(@ts-(ignore|expect-error|nocheck|check)\b|prettier-ignore\s*$)/;
const reference =
  /^\/\s*<reference\s+(path|types|lib|no-default-lib)=("[^"]*"|'[^']*')\s*\/>\s*$/;

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
      Program(program) {
        const code = program.body[0]?.range[0] ?? Infinity;
        for (const comment of context.sourceCode.getAllComments()) {
          if (
            comment.type === "Shebang" ||
            directive.test(comment.value) ||
            (comment.type === "Line" &&
              comment.range[0] < code &&
              reference.test(comment.value))
          )
            continue;
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
    linterOptions: { noInlineConfig: true },
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
