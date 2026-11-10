import { Link, useLoaderData } from "react-router";

export function ProviderCallbackError({ provider }: { provider: "Google" | "Discord" }) {
  const data = useLoaderData() as {
    error: { code: string; message: string } | null;
  };
  return (
    <main
      id="main-content"
      tabIndex={-1}
      className="page-shell narrow-shell"
      role="alert"
    >
      <p className="eyebrow">Clash Lens</p>
      <h1>Sign-in could not be completed</h1>
      <p>
        {data.error?.message ?? `${provider} sign-in could not be completed. Try again.`}
      </p>
      <Link className="button button-primary" to="/login">
        Try signing in again
      </Link>
    </main>
  );
}
