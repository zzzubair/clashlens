import { useLocation, useNavigation } from "react-router";

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

function decoded(path: string): string {
  try {
    return decodeURIComponent(path);
  } catch {
    return path;
  }
}
