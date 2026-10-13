import { useRouteLoaderData } from "react-router";

import { AnalyticsChoicesButton, useAnalyticsChoice } from "../components/Analytics";
import { DISCORD_INVITE_URL } from "../lib/discord";
import type { RootLoaderData } from "../root";

export function meta() {
  return [
    { title: "Privacy · Clash Lens" },
    {
      name: "description",
      content:
        "What Clash Lens counts about visits if you accept, and how to change your choice.",
    },
  ];
}

const CHOICE_TEXT = {
  accepted: "You accepted visitor counting in this browser.",
  declined: "You declined visitor counting in this browser.",
};

/** GET /privacy — plain-words notice of who runs Clash Lens, visitor counting and the choice about it. */
export default function Privacy() {
  const counting = Boolean(useRouteLoaderData<RootLoaderData>("root")?.analytics);
  const choice = useAnalyticsChoice();
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="privacy-title">
        <h1 id="privacy-title">Privacy</h1>
        <p className="hero-copy">
          What Clash Lens counts about visits if you accept, and how to change your
          choice.
        </p>
      </section>

      <section aria-labelledby="privacy-who">
        <h2 id="privacy-who">Who runs Clash Lens</h2>
        <p>
          Clash Lens, run by Zubair Shaik. Contact:{" "}
          <a href="mailto:privacy@clashlens.net">privacy@clashlens.net</a>
        </p>
      </section>

      <section aria-labelledby="privacy-counts">
        <h2 id="privacy-counts">Visitor counts</h2>
        {counting ? null : (
          <p role="status">Visitor counting is not switched on at the moment.</p>
        )}
        <p>
          We ask before counting anything. Until you accept, PostHog, a product analytics
          service, does not load and nothing about your visit is sent to it. If you
          decline, it never loads.
        </p>
        <p>
          If you accept, we use PostHog to count which pages and features get used so we
          can improve Clash Lens. PostHog keeps the data in its EU cloud in Frankfurt and
          handles it only for us.
        </p>
        <p>
          Each count says which page you opened, with player tags, usernames and other IDs
          removed from its address; a few actions, such as searching or changing a filter,
          but never what you typed; whether you are signed in; for one page load in ten,
          roughly how long it took; and the website that sent you here, without its full
          address.
        </p>
        <p>
          To see whether people come back, PostHog keeps a random ID in this
          browser&apos;s local storage. The ID is not linked to your Clash Lens, Google or
          Discord account. PostHog receives your IP address with each count, as every
          website you visit does. We tell PostHog not to work out your location.
        </p>
      </section>

      <section aria-labelledby="privacy-choice">
        <h2 id="privacy-choice">Your choice</h2>
        <p>
          This browser remembers whether you accepted or declined, so we don&apos;t ask on
          every visit. Change it at any time with the button below or with Analytics
          choices at the bottom of every page. Changing to Decline deletes PostHog&apos;s
          ID from this browser and stops counting straight away. Clearing this site&apos;s
          data in your browser means we ask again.
        </p>
        {counting ? (
          <p>
            {choice ? CHOICE_TEXT[choice] : "You have not chosen yet."}{" "}
            <AnalyticsChoicesButton>Change your analytics choice</AnalyticsChoicesButton>
          </p>
        ) : null}
      </section>

      <section aria-labelledby="privacy-server">
        <h2 id="privacy-server">Our server</h2>
        <p>
          Our server sends nothing to PostHog; only your browser does, and only after you
          accept. If you sign in with Google or Discord, we keep that service&apos;s ID
          for your account, never your email address, so you can sign back in. Visitor
          counts never use it.
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
          We count visits only with your consent, which you can withdraw at any time as
          above. Our PostHog plan keeps events for one year. Under UK data protection law
          you can ask to see or delete data about you, or object to its use: email{" "}
          <a href="mailto:privacy@clashlens.net">privacy@clashlens.net</a>, or ask us on{" "}
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
