// Interface translations without a dependency: the English text is the key,
// a catalogue maps it to another language, and anything missing falls back
// to English. Catalogues load only when their language is chosen, so the
// English interface pays nothing.
//
//   t("Refresh view")                           plain text
//   t("Monitored: {mode}", { mode })            interpolation
//   tn(count, "{count} chapter", "{count} chapters")   plurals (CLDR rules)
//   msg("Comics")                               marks a string kept in a data
//                                               table; translate it with t()
//                                               where it is rendered
//
// The language is a browser preference (tankarr.locale in localStorage, or
// the browser's languages); changing it reloads the page so that every
// module sees one catalogue for its whole life.
import { useEffect, useState, type ReactNode } from "react";

export type Locale = "en" | "it";
export type LocaleChoice = Locale | "auto";

export const LOCALES: { code: Locale; name: string }[] = [
  { code: "en", name: "English" },
  { code: "it", name: "Italiano" },
];
export const LOCALE_STORAGE_KEY = "tankarr.locale";

type PluralForms = Partial<Record<Intl.LDMLPluralRule, string>>;
export type Catalog = Record<string, string | PluralForms>;

const loaders: Record<Locale, () => Promise<Catalog>> = {
  en: () => Promise.resolve({}),
  it: () => import("./locales/it.json").then((module) => module.default as Catalog),
};

let currentLocale: Locale = "en";
let catalog: Catalog = {};
let pluralRules = new Intl.PluralRules("en");

export function locale(): Locale {
  return currentLocale;
}

function supported(code: string): Locale | null {
  const language = code.toLowerCase().split(/[-_]/)[0];
  return LOCALES.some((item) => item.code === language) ? (language as Locale) : null;
}

export function storedLocale(): LocaleChoice {
  try {
    const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY);
    return stored && supported(stored) ? (stored as Locale) : "auto";
  } catch {
    return "auto";
  }
}

/** The language the browser asks for, ignoring any stored choice. */
export function browserLocale(languages: readonly string[] = navigator.languages ?? [navigator.language]): Locale {
  for (const language of languages) {
    const match = supported(language);
    if (match) return match;
  }
  return "en";
}

export function detectLocale(languages: readonly string[] = navigator.languages ?? [navigator.language]): Locale {
  const stored = storedLocale();
  return stored !== "auto" ? stored : browserLocale(languages);
}

export function saveLocale(choice: LocaleChoice): void {
  try {
    if (choice === "auto") window.localStorage.removeItem(LOCALE_STORAGE_KEY);
    else window.localStorage.setItem(LOCALE_STORAGE_KEY, choice);
  } catch {
    // Private windows may refuse storage; the choice then lasts for this page.
  }
}

/** Load a catalogue and make it current. Tests call it directly. */
export async function activate(next: Locale, preloaded?: Catalog): Promise<void> {
  catalog = preloaded ?? (next === "en" ? {} : await loaders[next]());
  currentLocale = next;
  pluralRules = new Intl.PluralRules(next);
  if (typeof document !== "undefined") document.documentElement.lang = next;
}

function interpolate(text: string, params?: Record<string, unknown>): string {
  if (!params) return text;
  return text.replace(/\{(\w+)\}/g, (match, key: string) =>
    key in params ? String(params[key]) : match,
  );
}

export function t(text: string, params?: Record<string, unknown>): string {
  const entry = catalog[text];
  const translated = typeof entry === "string" ? entry : entry?.other ?? text;
  return interpolate(translated, params);
}

export function tn(count: number, one: string, other: string, params?: Record<string, unknown>): string {
  const entry = catalog[other];
  let form: string;
  if (entry && typeof entry === "object") {
    form = entry[pluralRules.select(count)] ?? entry.other ?? (count === 1 ? one : other);
  } else if (typeof entry === "string") {
    form = entry;
  } else {
    form = count === 1 ? one : other;
  }
  return interpolate(form, { count, ...params });
}

/** Marks a string in a data table for extraction; render it through t(). */
export function msg(text: string): string {
  return text;
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(() => detectLocale() === "en");
  useEffect(() => {
    const wanted = detectLocale();
    if (wanted === "en") return;
    let cancelled = false;
    activate(wanted)
      .catch(() => activate("en"))
      .then(() => {
        if (!cancelled) setReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return ready ? <>{children}</> : null;
}
