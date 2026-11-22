import { useLocation, useNavigation } from "react-router";

/**
 * The fields of the form this page is posting to `action` (its own address
 * by default), from the moment it is sent until the page that answers it has
 * loaded. Its button stays busy for that whole time, so a Clasher cannot send
 * it again while the first one is still finishing.
 */
export function usePendingForm(action?: string): FormData | undefined {
  const navigation = useNavigation();
  const { pathname } = useLocation();
  const { formAction, formData, formMethod } = navigation;
  if (navigation.state === "idle" || !formAction || formMethod?.toUpperCase() === "GET") {
    return undefined;
  }
  const destination = new URL(formAction, "http://localhost").pathname;
  return decoded(destination) === decoded(action ?? pathname) ? formData : undefined;
}

// Player tags appear as %23 in some addresses and # in others.
function decoded(path: string): string {
  try {
    return decodeURIComponent(path);
  } catch {
    return path;
  }
}
