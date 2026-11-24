import { needsAccountText } from "./NeedsAccount";

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
