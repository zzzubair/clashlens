import { needsAccountText } from "./NeedsAccount";

/**
 * Where a signed-out Clasher was going, in one line on Sign in and Create
 * account: the page's name and why it needs an account, as the page itself
 * said. Nothing for pages anyone can open, the bare account page every
 * sign-in comes back to by default, or a crew invite (named on its own).
 */
export function SignInReason({ returnPath }: { returnPath: string }) {
  const path = returnPath.split(/[?#]/)[0];
  if (!/^\/(crews|account\/.)/.test(path) || path.startsWith("/crews/join/")) return null;
  const { title, reason } = needsAccountText(path);
  return (
    <p className="sign-in-reason">
      <strong>{title}.</strong> {reason}
    </p>
  );
}
