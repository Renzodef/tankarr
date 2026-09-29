import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Icon } from "../components";
import { t } from "../i18n";

type SeriesFilterProps = {
  value: string;
  series: readonly string[];
  onChange: (value: string) => void;
  placeholder: string;
  ariaLabel: string;
};

function normalized(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/\p{Diacritic}/gu, "")
    .toLocaleLowerCase();
}

const titleCollator = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });
const MAX_VISIBLE_CHOICES = 50;

export function SeriesFilter({
  value,
  series,
  onChange,
  placeholder,
  ariaLabel,
}: SeriesFilterProps) {
  const root = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);

  const titles = useMemo(() => {
    const unique = new Map<string, string>();
    series.forEach((title) => {
      const trimmed = title.trim();
      if (trimmed) unique.set(normalized(trimmed), trimmed);
    });
    return [...unique.entries()].map(([text, title]) => ({ text, title }))
      .sort((left, right) => titleCollator.compare(left.title, right.title));
  }, [series]);
  const matchingTitles = useMemo(() => {
    const query = normalized(value.trim());
    if (!query) return titles;
    return titles.filter((item) => item.text.includes(query));
  }, [titles, value]);
  const visibleTitles = matchingTitles.slice(0, MAX_VISIBLE_CHOICES);
  const choices = ["", ...visibleTitles.map((item) => item.title)];

  useEffect(() => {
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, []);

  useEffect(() => {
    setActiveIndex(-1);
  }, [value]);

  const select = (next: string) => {
    onChange(next);
    setOpen(false);
    setActiveIndex(-1);
    window.requestAnimationFrame(() => input.current?.focus());
  };

  return (
    <div className="series-filter" ref={root}>
      <Icon name="search" size={16} />
      <input
        ref={input}
        className="input"
        type="search"
        role="combobox"
        aria-label={ariaLabel}
        aria-autocomplete="list"
        aria-controls={listId}
        aria-expanded={open}
        aria-activedescendant={activeIndex >= 0 ? `${listId}-option-${activeIndex}` : undefined}
        placeholder={placeholder}
        value={value}
        onFocus={() => setOpen(true)}
        onChange={(event) => {
          onChange(event.target.value);
          setOpen(true);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            // Search inputs clear themselves on Escape in Chromium. Closing
            // the suggestions must preserve the current list filter.
            event.preventDefault();
            setOpen(false);
            setActiveIndex(-1);
            return;
          }
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setOpen(true);
            setActiveIndex((current) => Math.min(current + 1, choices.length - 1));
            return;
          }
          if (event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
            setActiveIndex((current) => current <= 0 ? choices.length - 1 : current - 1);
            return;
          }
          if (event.key === "Enter" && open && activeIndex >= 0) {
            event.preventDefault();
            select(choices[activeIndex]);
          }
        }}
      />
      <button
        type="button"
        className="series-filter-toggle"
        aria-label={open ? t("Hide available series") : t("Show available series")}
        aria-expanded={open}
        aria-controls={listId}
        title={open ? t("Hide available series") : t("Show available series")}
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => {
          setOpen((current) => !current);
          window.requestAnimationFrame(() => input.current?.focus());
        }}
      >
        <Icon name="chevronDown" size={17} />
      </button>
      {open ? (
        <ul className="series-filter-menu" id={listId} role="listbox" aria-label={t("Available series")}>
          <li
            id={`${listId}-option-0`}
            className={`series-filter-option${activeIndex === 0 ? " is-active" : ""}${!value.trim() ? " is-selected" : ""}`}
            role="option"
            aria-selected={!value.trim()}
            onClick={() => select("")}
          >
            {t("All series")}
          </li>
          {visibleTitles.map(({ title }, index) => (
            <li
              id={`${listId}-option-${index + 1}`}
              className={`series-filter-option${activeIndex === index + 1 ? " is-active" : ""}${value === title ? " is-selected" : ""}`}
              key={title}
              role="option"
              aria-selected={value === title}
              onClick={() => select(title)}
            >
              {title}
            </li>
          ))}
          {matchingTitles.length > MAX_VISIBLE_CHOICES ? (
            <li className="series-filter-empty">{t("Type to narrow {count} matching series.", { count: matchingTitles.length })}</li>
          ) : null}
          {!matchingTitles.length ? <li className="series-filter-empty">{t("No matching series")}</li> : null}
        </ul>
      ) : null}
    </div>
  );
}
