import { useRouteLoaderData } from "react-router";

import { ReturnVisitsSwitch } from "../components/Analytics";
import { DISCORD_INVITE_URL } from "../lib/discord";
import type { RootLoaderData } from "../root";

export function meta() {
  return [
    { title: "Privacy · Clash Lens" },
    {
      name: "description",
      content: "What Clash Lens counts about visits, and how to turn return visits off.",
    },
  ];
}

/** GET /privacy — plain-words notice for visitor counting and its off switch. */
export default function Privacy() {
  const counting = Boolean(useRouteLoaderData<RootLoaderData>("root")?.analytics);
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="privacy-title">
        <h1 id="privacy-title">Privacy</h1>
        <p className="hero-copy">
          What Clash Lens counts about visits, and how to turn part of it off.
        </p>
      </section>

      <section aria-labelledby="privacy-counts">
        <h2 id="privacy-counts">Visitor counts</h2>
        {counting ? null : (
          <p role="status">Visitor counting is not switched on at the moment.</p>
        )}
        <p>
          We use PostHog, a product analytics service, to count which pages and features
          get used so we can improve Clash Lens. PostHog keeps the data in its EU cloud in
          Frankfurt and handles it only for us.
        </p>
        <p>
          Each count says which page you opened, with player tags, usernames and other IDs
          removed from its address; a few actions, such as searching or changing a filter,
          but never what you typed; whether you are signed in; the website that sent you
          here, without its full address; and your browser, device type, screen size,
          language and time zone.
        </p>
        <p>
          Everyone is counted without cookies. PostHog receives your IP address with each
          count, as every website you visit does. Its server mixes it with your browser
          details and a secret that changes every day into a code that tells one
          day&apos;s visitors apart; it cannot link visits on different days. We tell
          PostHog not to work out your location.
        </p>
      </section>

      <section aria-labelledby="privacy-return-visits">
        <h2 id="privacy-return-visits">Return visits</h2>
        <p>
          To see whether people come back, this browser also keeps a random ID in its
          local storage. It is on unless you turn it off. The ID is not linked to your
          Clash Lens, Google or Discord account, and it is replaced with a new one after
          13 months.
        </p>
        <p>
          Turn it off with this switch or the one at the bottom of every page. That
          deletes the ID from this browser straight away and this browser remembers your
          choice. Your visits are then still counted without cookies, as above. Clearing
          this site&apos;s data in your browser turns the switch back on.
        </p>
        <p>
          <ReturnVisitsSwitch />
        </p>
      </section>

      <section aria-labelledby="privacy-server">
        <h2 id="privacy-server">Accounts and groups</h2>
        <p>
          When someone creates an account or a group, our server sends PostHog a count
          with no account, name or group details. If you sign in with Google or Discord,
          we keep that service&apos;s ID for your account, never your email address, so
          you can sign back in. Visitor counts never use it.
        </p>
      </section>

      <section aria-labelledby="privacy-never">
        <h2 id="privacy-never">What we never do</h2>
        <p>
          No ads, no selling data, no screen recordings, no heatmaps, and no following you
          around other websites.
        </p>
      </section>

      <section aria-labelledby="privacy-rights">
        <h2 id="privacy-rights">How long, and your rights</h2>
        <p>
          We count visits because we have a legitimate interest in improving Clash Lens.
          Our PostHog plan keeps events for one year. Under UK data protection law you can
          ask to see or delete data about you, or object to its use: ask us on{" "}
          <a href={DISCORD_INVITE_URL} target="_blank" rel="noopener noreferrer">
            our Discord server
          </a>
          . You can also complain to the Information Commissioner&apos;s Office at{" "}
          <a href="https://ico.org.uk/make-a-complaint/">ico.org.uk</a>.
        </p>
      </section>
    </main>
  );
}
