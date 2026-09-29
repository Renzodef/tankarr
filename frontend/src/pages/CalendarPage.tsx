import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { EmptyState, Icon, Spinner, chapterLabel, seriesPath, useApp } from "../components";
import type {
  CalendarAvailabilityStatus,
  CalendarRelease,
  CalendarResponse,
  ExpectedRelease,
} from "../types";
import { locale, msg, t, tn } from "../i18n";

type CalendarStatus = CalendarAvailabilityStatus;

type CalendarEvent = {
  key: string;
  day: string;
  mangaId: string;
  mangaTitle: string;
  chapter: string;
  detail: string;
  status: CalendarStatus;
};

type CalendarWindow = {
  start: string;
  end: string;
  payload: CalendarResponse;
  fetchedAt: number;
};

const CALENDAR_WINDOW_DAYS = 28;
const CALENDAR_CACHE_TTL_MS = 2 * 60 * 1000;
const CALENDAR_CACHE_LIMIT = 8;
const calendarWindowCache = new Map<string, CalendarWindow>();
const calendarWindowRequests = new Map<string, Promise<CalendarWindow>>();

const STATUS_LABELS: Record<CalendarStatus, string> = {
  downloaded: msg("Downloaded"),
  official_available: msg("Official available"),
  early_available: msg("Early release available"),
  expected: msg("Expected"),
};

function localDate(value: string): Date {
  const [year, month, day] = value.slice(0, 10).split("-").map(Number);
  return new Date(year, month - 1, day, 12);
}

function dayKey(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function addDays(date: Date, amount: number): Date {
  const shifted = new Date(date);
  shifted.setDate(shifted.getDate() + amount);
  return shifted;
}

function startOfWeek(date: Date): Date {
  const start = new Date(date);
  const daysSinceMonday = (start.getDay() + 6) % 7;
  start.setDate(start.getDate() - daysSinceMonday);
  start.setHours(12, 0, 0, 0);
  return start;
}

function calendarWindowRange(weekStart: string): { start: string; end: string } {
  const selected = localDate(weekStart);
  const start = dayKey(addDays(selected, -7));
  return {
    start,
    end: dayKey(addDays(localDate(start), CALENDAR_WINDOW_DAYS - 1)),
  };
}

function cachedWindowForWeek(weekStart: string): CalendarWindow | undefined {
  const weekEnd = dayKey(addDays(localDate(weekStart), 6));
  return [...calendarWindowCache.values()]
    .filter((entry) => entry.start <= weekStart && entry.end >= weekEnd)
    .sort((left, right) => right.fetchedAt - left.fetchedAt)[0];
}

function retainCalendarWindow(window: CalendarWindow): void {
  const key = `${window.start}:${window.end}`;
  calendarWindowCache.delete(key);
  calendarWindowCache.set(key, window);
  while (calendarWindowCache.size > CALENDAR_CACHE_LIMIT) {
    const oldest = calendarWindowCache.keys().next().value;
    if (!oldest) break;
    calendarWindowCache.delete(oldest);
  }
}

function fetchCalendarWindow(weekStart: string, fresh = false): Promise<CalendarWindow> {
  const cached = cachedWindowForWeek(weekStart);
  if (!fresh && cached && Date.now() - cached.fetchedAt < CALENDAR_CACHE_TTL_MS) {
    return Promise.resolve(cached);
  }
  const range = calendarWindowRange(weekStart);
  const key = `${range.start}:${range.end}`;
  const pending = calendarWindowRequests.get(key);
  if (pending) return pending;
  const request = api
    .calendar(range.start, range.end)
    .then((payload) => {
      const window = { ...range, payload, fetchedAt: Date.now() };
      retainCalendarWindow(window);
      return window;
    })
    .finally(() => calendarWindowRequests.delete(key));
  calendarWindowRequests.set(key, request);
  return request;
}

function prefetchCalendarEdge(weekStart: string, window: CalendarWindow): void {
  const weekEnd = dayKey(addDays(localDate(weekStart), 6));
  if (weekStart === window.start) {
    void fetchCalendarWindow(dayKey(addDays(localDate(weekStart), -7))).catch(() => undefined);
  } else if (weekEnd === window.end) {
    void fetchCalendarWindow(dayKey(addDays(localDate(weekStart), 7))).catch(() => undefined);
  }
}

function releaseDay(value: string): string {
  if (value.length <= 10) return value.slice(0, 10);
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value.slice(0, 10) : dayKey(parsed);
}

function rangeLabel(start: Date, end: Date): string {
  const left = new Intl.DateTimeFormat(locale(), {
    month: "short",
    day: "numeric",
    ...(start.getFullYear() !== end.getFullYear() ? { year: "numeric" } : {}),
  }).format(start);
  const right = new Intl.DateTimeFormat(locale(), {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(end);
  return `${left} \u2013 ${right}`;
}

function eventStatus(item: ExpectedRelease): CalendarStatus {
  return item.availability_status;
}

function releasedEvent(item: CalendarRelease): CalendarEvent | null {
  if (!item.publish_at) return null;
  const chapter = chapterLabel(item.volume, item.chapter);
  // Sources decorate titles with their state (a lock for a paid episode on
  // Tapas); the badge is the source's, not the chapter's name.
  const title = item.title.replace(/[\u{1F510}-\u{1F513}\u{1F525}\u{1F195}\u{2B50}\u{FE0F}]/gu, "").trim();
  const moment = new Date(item.publish_at);
  const time = Number.isNaN(moment.getTime())
    ? ""
    : new Intl.DateTimeFormat(locale(), { hour: "2-digit", minute: "2-digit" }).format(moment);
  const detail = title && title !== chapter ? title : t("Released");
  return {
    key: `release:${item.id}`,
    day: releaseDay(item.publish_at),
    mangaId: item.manga_id,
    mangaTitle: item.manga_title,
    chapter,
    detail: time ? `${detail} · ${time}` : detail,
    status: item.availability_status,
  };
}

function expectedEvent(item: ExpectedRelease): CalendarEvent {
  const overdue = item.overdue_days ?? 0;
  return {
    key: `expected:${item.manga_id}:${item.chapter}`,
    day: item.expected_at,
    mangaId: item.manga_id,
    mangaTitle: item.manga_title,
    chapter: t("Chapter {number}", { number: item.chapter }),
    // The date is projected from the work's release rhythm, never announced
    // by the publisher: say so, and say when the author skipped a slot.
    detail:
      overdue > 0
        ? tn(overdue, "{cadence} rhythm · {count} day overdue", "{cadence} rhythm · {count} days overdue", { cadence: item.cadence_label })
        : t("{cadence} rhythm", { cadence: item.cadence_label }),
    status: eventStatus(item),
  };
}

export default function CalendarPage() {
  const { notify } = useApp();
  const [weekStart, setWeekStart] = useState(() => dayKey(startOfWeek(new Date())));
  const [releases, setReleases] = useState<CalendarRelease[] | null>(null);
  const [expected, setExpected] = useState<ExpectedRelease[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(true);

  const days = useMemo(() => {
    const first = localDate(weekStart);
    return Array.from({ length: 7 }, (_, index) => addDays(first, index));
  }, [weekStart]);
  const weekEnd = dayKey(days[6]);

  useEffect(() => {
    let current = true;
    const cached = cachedWindowForWeek(weekStart);
    if (cached) {
      setReleases(cached.payload.releases);
      setExpected(cached.payload.expected);
      setLoading(false);
    } else {
      setLoading(true);
    }
    let pending = false;
    const refresh = async () => {
      if (!current || pending || document.hidden) return;
      pending = true;
      setRefreshing(true);
      try {
        // Cached windows make navigation immediate, but publication status
        // and downloads can change while this page remains open.
        const window = await fetchCalendarWindow(weekStart, true);
        if (!current) return;
        setReleases(window.payload.releases);
        setExpected(window.payload.expected);
        prefetchCalendarEdge(weekStart, window);
      } catch (caught) {
        if (current) notify("error", String(caught));
      } finally {
        pending = false;
        if (current) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), CALENDAR_CACHE_TTL_MS);
    const wake = () => void refresh();
    window.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", wake);
    return () => {
      current = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [notify, weekEnd, weekStart]);

  const events = useMemo(() => {
    const actual: CalendarEvent[] = [];
    const releasedChapters = new Set<string>();
    for (const release of releases ?? []) {
      const event = releasedEvent(release);
      if (!event) continue;
      actual.push(event);
      releasedChapters.add(`${release.manga_id}:${release.chapter ?? release.volume ?? ""}`);
    }
    const predictions = expected
      .filter((item) => !releasedChapters.has(`${item.manga_id}:${item.chapter}`))
      .map(expectedEvent);
    return [...actual, ...predictions]
      .filter((item) => item.day >= weekStart && item.day <= weekEnd)
      .sort((left, right) =>
        left.day === right.day
          ? left.mangaTitle.localeCompare(right.mangaTitle)
          : left.day.localeCompare(right.day),
      );
  }, [expected, releases, weekEnd, weekStart]);

  const eventsByDay = useMemo(() => {
    const grouped = new Map<string, CalendarEvent[]>();
    for (const event of events) {
      const bucket = grouped.get(event.day);
      if (bucket) bucket.push(event);
      else grouped.set(event.day, [event]);
    }
    return grouped;
  }, [events]);

  const today = dayKey(new Date());
  const currentWeek = today >= weekStart && today <= weekEnd;
  const shiftWeek = (amount: number) => setWeekStart(dayKey(addDays(localDate(weekStart), amount)));
  const returnToToday = () => setWeekStart(dayKey(startOfWeek(new Date())));

  return (
    <div className="page calendar-page">
      <div className="calendar-titlebar">
        <div>
          <h1 className="page-title">{t("Calendar")}</h1>
          <p className="calendar-summary">
            {tn(events.length, "{count} release this week", "{count} releases this week")}
          </p>
        </div>
      </div>

      <div className="calendar-controls">
        <div className="calendar-navigation" aria-label={t("Calendar navigation")}>
          <button
            type="button"
            className="btn btn-icon calendar-previous"
            title={t("Previous week")}
            aria-label={t("Previous week")}
            onClick={() => shiftWeek(-7)}
          >
            <Icon name="chevronRight" />
          </button>
          <button
            type="button"
            className="btn btn-icon"
            title={t("Next week")}
            aria-label={t("Next week")}
            onClick={() => shiftWeek(7)}
          >
            <Icon name="chevronRight" />
          </button>
          <button type="button" className="btn" disabled={currentWeek} onClick={returnToToday}>
            {t("Today")}
          </button>
        </div>
        <h2 className="calendar-range">{rangeLabel(days[0], days[6])}</h2>
        <span className={`calendar-refresh-state${refreshing ? " is-loading" : ""}`} aria-live="polite">
          {refreshing ? t("Updating") : ""}
        </span>
      </div>

      {releases === null && loading ? (
        <Spinner />
      ) : events.length === 0 && !loading ? (
        <EmptyState
          icon="calendar"
          title={t("No releases this week")}
          hint={t("No monitored official releases are dated in this interval.")}
        />
      ) : (
        <div className={`calendar-week${loading ? " is-loading" : ""}`} aria-busy={loading}>
          {days.map((date) => {
            const key = dayKey(date);
            const dayEvents = eventsByDay.get(key) ?? [];
            const isToday = key === today;
            return (
              <section
                key={key}
                className={`calendar-column${isToday ? " is-today" : ""}${dayEvents.length === 0 ? " is-empty" : ""}`}
              >
                <header className="calendar-day-header">
                  <span className="calendar-weekday">
                    {new Intl.DateTimeFormat(locale(), { weekday: "short" }).format(date)}
                  </span>
                  <time dateTime={key} className="calendar-date">
                    {new Intl.DateTimeFormat(locale(), { month: "numeric", day: "numeric" }).format(date)}
                  </time>
                  {isToday ? <span className="calendar-today-label">{t("Today")}</span> : null}
                </header>
                <div className="calendar-events">
                  {dayEvents.map((event) => (
                    <a
                      key={event.key}
                      className={`calendar-event calendar-event-${event.status}`}
                      href={seriesPath(event.mangaId)}
                      title={`${event.mangaTitle} - ${event.chapter} - ${t(STATUS_LABELS[event.status])}`}
                    >
                      <span className="calendar-event-title">{event.mangaTitle}</span>
                      <span className="calendar-event-chapter">{event.chapter}</span>
                      <span className="calendar-event-detail">{event.detail}</span>
                      <span className="calendar-event-status">{STATUS_LABELS[event.status]}</span>
                    </a>
                  ))}
                </div>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
