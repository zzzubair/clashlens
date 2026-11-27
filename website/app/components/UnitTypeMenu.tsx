import { useNavigation } from "react-router";

/** A phone's one menu for the unit types, in the filter form in place of the
 * tabs, so it submits with Players and works without JavaScript. It shows a
 * picked type while that type's results load, and stays the same element,
 * so keyboard focus stays on it. */
export function UnitTypeMenu({
  choices,
  category,
}: {
  choices: [value: string, label: string][];
  category: string;
}) {
  const navigation = useNavigation();
  const shown =
    new URLSearchParams(navigation.location?.search).get("category") ?? category;
  return (
    <label className="analytics-field analytics-unit-menu">
      <span>Unit type</span>
      <select name="category" value={shown} onChange={() => {}}>
        {choices.map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>
    </label>
  );
}
