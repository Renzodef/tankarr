import { useMemo, useState, type ReactNode } from "react";
import { Icon, StatusPill } from "../components";
import type { ReaderLink, SeriesBookGroup, SeriesUnitChapter, SeriesUnits, VolumeMonitorState } from "../types";

type UnitFilter = "all" | "problems" | "missing" | "downloaded" | "monitored";
type UnitOrder = "ascending" | "descending";
type PlanState = "book" | "assemble" | "partial" | "missing";

export type GroupActions = {
  busy: boolean;
  monitoringLocked?: boolean;
  readerLabel: string;
  bookmark?: ReaderLink["bookmark"];
  readerUrl: (releaseId: string) => string | undefined;
  onSetBookMonitoring: (book: SeriesBookGroup, state: VolumeMonitorState) => void;
  onAutomaticSearchBook: (book: SeriesBookGroup) => void;
  onSearchBook: (book: SeriesBookGroup) => void;
  onDeleteBookFiles: (book: SeriesBookGroup) => void;
  onRetireDuplicates: (book: SeriesBookGroup) => void;
  renderChapterRows: (chapters: SeriesUnitChapter[], book: SeriesBookGroup | null) => ReactNode;
};

function planOf(book: SeriesBookGroup): PlanState {
  if (book.plan_state) return book.plan_state;
  if (book.owned) return "book";
  if (book.chapter_count > 0 && book.downloaded_chapter_count >= book.chapter_count) return "assemble";
  if (book.downloaded_chapter_count > 0) return "partial";
  return "missing";
}
function isProblem(book: SeriesBookGroup): boolean {
  const plan = planOf(book);
  return !book.ignored && plan !== "book" && plan !== "assemble";
}
function matchesChapter(chapter: SeriesUnitChapter, filter: UnitFilter): boolean {
  if (filter === "missing") return chapter.missing;
  if (filter === "downloaded") return chapter.downloaded;
  if (filter === "monitored") return chapter.monitored && !chapter.ignored;
  return true;
}
function matchesBook(book: SeriesBookGroup, filter: UnitFilter): boolean {
  if (filter === "problems") return isProblem(book);
  if (filter === "missing") return book.expected && !book.ignored && ["missing", "unmapped"].includes(book.status);
  if (filter === "downloaded") return book.owned;
  if (filter === "monitored") return book.monitored && !book.ignored;
  return true;
}
function orderedRows<T>(items: T[], order: UnitOrder): T[] {
  return order === "descending" ? [...items].reverse() : items;
}
const PLAN_LABEL: Record<PlanState, string> = { book: "Book", assemble: "From chapters", partial: "In part", missing: "Missing" };
const PLAN_KIND: Record<PlanState, string> = { book: "success", assemble: "info", partial: "warn", missing: "danger" };

function ChapterTable({ chapters, book, order, renderRows }: { chapters: SeriesUnitChapter[]; book: SeriesBookGroup | null; order: UnitOrder; renderRows: GroupActions["renderChapterRows"] }) {
  const ordered = orderedRows(chapters, order);
  return <div className="data-table-frame">
    <table className="table chapter-table">
      <thead><tr><th className="col-monitor" aria-label="Monitored" /><th className="col-chapter">Chapter</th><th className="col-status">Status</th><th className="col-actions" aria-label="Actions" /></tr></thead>
      <tbody>{renderRows(ordered, book)}</tbody>
    </table>
  </div>;
}

function BookRow({ book, chapters, filter, order, ...actions }: GroupActions & {
  book: SeriesBookGroup; chapters: SeriesUnitChapter[]; filter: UnitFilter; order: UnitOrder;
}) {
  const plan = planOf(book);
  const [collapsedOverride, setCollapsedOverride] = useState<boolean | null>(null);
  const collapsed = collapsedOverride ?? (book.owned || (filter === "all" && plan === "book"));
  // Owned volumes have the same presentation regardless of source chapter indexing.
  // Separate releases remain an explicit secondary view, never an implied contents list.
  const visibleChapters = book.owned
    ? chapters.filter((chapter) => chapter.releases.length > 0 || chapter.files.length > 0 || chapter.downloaded)
    : chapters;
  const canExpand = book.owned ? visibleChapters.length > 0 : book.chapter_count > 0 || chapters.length > 0;
  const url = book.open_release_id ? actions.readerUrl(book.open_release_id) : undefined;
  const bookmark = actions.bookmark;
  const containsBookmark = bookmark && (
    book.open_release_id === bookmark.chapter_id
    || book.files.some((file) => file.id === bookmark.chapter_id)
    || book.chapters.some((chapter) => chapter.releases.some((release) => release.id === bookmark.chapter_id))
  );
  const range = book.chapter_range ? `ch. ${book.chapter_range.first}–${book.chapter_range.last}` : "chapters not known";
  const text = book.plan_text
    ?? (plan === "book" ? (book.pages ? `${book.pages} pages` : "On disk")
      : plan === "assemble" ? "All chapters on disk"
        : plan === "partial" ? `${book.downloaded_chapter_count} of ${book.chapter_count} chapters`
          : "Search the book, then chapters");
  const boundarySource = book.map_sources?.includes("content") ? "pages matched to the book" : book.map_sources?.includes("source_tags") ? "from the source's volume tags" : book.estimated ? null : book.map_sources?.includes("ocr") ? "read from book" : book.map_sources?.includes("operator") ? "saved boundaries" : "catalogue boundaries";
  const description = book.owned
    ? `${book.chapter_range ? `${range} · ` : ""}${text}`
    : `${range} · ${text}`;
  const detail = book.owned && book.chapter_range ? [range, boundarySource, text].filter(Boolean).join(" · ") : description;
  const primary = plan === "missing" || plan === "partial"
      ? <button type="button" className="btn btn-small" disabled={actions.busy} aria-label={`Automatically search book ${book.volume}`} onClick={() => actions.onAutomaticSearchBook(book)}>Search</button>
      : null;
  return <section className={`book-row${book.estimated && !book.owned ? " is-estimated" : ""}${book.ignored ? " is-ignored" : ""}`} aria-label={`Book ${book.volume}`}>
    <div className="book-row-line">
      {canExpand && !book.owned
        ? <button type="button" className="volume-toggle" aria-expanded={!collapsed} aria-label={`${collapsed ? "Expand" : "Collapse"} book ${book.volume}`} onClick={() => setCollapsedOverride(!collapsed)}><Icon name={collapsed ? "chevronRight" : "chevronDown"} /></button>
        : <span className="volume-toggle volume-toggle-empty" />}
      <h3 className="book-row-title">Book {book.volume}</h3>
      <div className="book-row-mid">
        <StatusPill kind={book.ignored ? "muted" : PLAN_KIND[plan]}>{book.ignored ? "Ignored" : PLAN_LABEL[plan]}</StatusPill>
        {containsBookmark ? <a className="bookmark-location" href={bookmark.url} target="_blank" rel="noreferrer">
          <Icon name="bookmark" size={14} /> {bookmark.label} · page {bookmark.page_index + 1} · Continue
        </a> : null}
        <span className="book-row-text muted small" title={detail}>
          {description}
        </span>
      </div>
      <div className="book-row-actions">
        {primary}
        {url ? <a className="btn btn-ghost btn-small btn-icon" href={url} target="_blank" rel="noreferrer" aria-label={`Read book ${book.volume} in ${actions.readerLabel}`} title={url.startsWith("#/") ? "Read" : `Read in ${actions.readerLabel}`}><Icon name="library" size={14} /></a> : null}
        <details className="row-menu">
          <summary className="btn btn-ghost btn-small btn-icon" aria-label={`More actions for book ${book.volume}`} title="More">⋯</summary>
          <div className="row-menu-panel">
            <label className="row-menu-item">Monitoring
              <select className="input volume-monitor-select" aria-label={`Monitoring for book ${book.volume}`} value={book.volume_monitor_state} disabled={actions.busy || actions.monitoringLocked} title={actions.monitoringLocked ? "The series is manually marked up to date" : undefined} onChange={(event) => actions.onSetBookMonitoring(book, event.target.value as VolumeMonitorState)}>
                <option value="automatic">Automatic</option><option value="monitored">Monitored</option><option value="ignored">Ignored</option>
              </select>
            </label>
            {book.owned && canExpand ? <button type="button" className="row-menu-item" aria-expanded={!collapsed} aria-label={`${collapsed ? "Show" : "Hide"} separate chapter releases for book ${book.volume}`} onClick={() => setCollapsedOverride(!collapsed)}>{collapsed ? "Show" : "Hide"} separate chapter releases</button> : null}
            {plan === "assemble" ? <button type="button" className="row-menu-item" disabled={actions.busy} aria-label={`Automatically search book ${book.volume}`} onClick={() => actions.onAutomaticSearchBook(book)}><Icon name="search" size={14} /> Search the book file</button> : null}
            <button type="button" className="row-menu-item" disabled={actions.busy} aria-label={`Interactive search for book ${book.volume}`} onClick={() => actions.onSearchBook(book)}><Icon name="user" size={14} /> Interactive search</button>
            {book.duplicate_file_count > 0 && book.duplicate_release_ids.length > 0 ? <button type="button" className="row-menu-item" disabled={actions.busy} aria-label={`Retire duplicate chapters in book ${book.volume}`} onClick={() => actions.onRetireDuplicates(book)}>Retire {book.duplicate_file_count} duplicate chapter file{book.duplicate_file_count === 1 ? "" : "s"}…</button> : null}
            {book.files.length > 0 ? <button type="button" className="row-menu-item" disabled={actions.busy} aria-label={`Delete files for book ${book.volume}`} onClick={() => actions.onDeleteBookFiles(book)}><Icon name="trash" size={14} /> Delete files…</button> : null}
          </div>
        </details>
      </div>
    </div>
    {book.redundant_book ? <p className="book-row-note muted small">This book is redundant: the whole series completes from chapters.</p> : null}
    {canExpand && !collapsed
      ? <div className="book-row-detail">
        {book.suspect && book.chapters.length > 0 ? <p className="muted small">{book.retirement_note || "Separate chapter files are preserved until their contents can be matched to this book."}</p> : null}
        {plan === "book" ? <p className="muted small book-row-plan">Book on disk. Separate chapter releases:</p> : null}
        {visibleChapters.length > 0 ? <ChapterTable key={filter} chapters={visibleChapters} book={book} order={order} renderRows={actions.renderChapterRows} /> : <p className="muted small">No chapters match this filter.</p>}
      </div>
      : null}
  </section>;
}

function describeConfidence(data: SeriesUnits): string {
  const confidence = data.map_confidence;
  if (!confidence) return data.mode === "grouped" ? "Book boundaries: exact map." : "Book boundaries: not known.";
  const sources = confidence.sources.length ? confidence.sources.join(", ") : "none";
  if (confidence.level === "exact") return confidence.sources.includes("operator")
    ? "Book boundaries: manually saved; editorial accuracy has not been independently verified."
    : `Book boundaries: mapped (${sources}).`;
  if (confidence.level === "estimated") return "Chapters are laid out evenly across the books.";
  if (confidence.level === "partial" && confidence.unknown_books) return `Book boundaries: ${sources}; ${confidence.unknown_books} book${confidence.unknown_books === 1 ? " has" : "s have"} no verified chapter assignment.`;
  if (confidence.level === "partial") return `Book boundaries: ${sources}; ${confidence.estimated_books} book${confidence.estimated_books === 1 ? "" : "s"} laid out between known neighbours.`;
  return "Book boundaries: not known.";
}

function CompletenessSummary({ data }: { data: SeriesUnits }) {
  const counts = data.completeness;
  if (!counts) return null;
  const chapters = [...data.books.flatMap((book) => book.chapters), ...data.unassigned_chapters];
  const mappedBookCoverage = counts.owned_books > 0 && counts.indexed_chapters > 0 && chapters.length > 0
    && chapters.every((chapter) => chapter.downloaded || Boolean(chapter.covered_by_volume));
  const unmappedBookCoverage = counts.owned_books > 0 && counts.indexed_chapters_on_disk < counts.indexed_chapters && !mappedBookCoverage;
  return <p className="muted small" aria-label="Library completeness">
    {mappedBookCoverage ? <><b>{counts.indexed_chapters}</b> chapters indexed; covered by the owned books. </>
      : unmappedBookCoverage ? <><b>{counts.indexed_chapters}</b> chapters indexed; their coverage inside the owned books is not fully mapped. </>
        : counts.indexed_chapters > 0 ? <>Indexed chapter content: <b>{counts.indexed_chapters_on_disk}/{counts.indexed_chapters}</b> on disk. </> : <>Chapter coverage is not enumerated. </>}
    Edition files: <b>{counts.owned_books}/{counts.expected_books ?? "?"}</b> books.
  </p>;
}

export default function SeriesUnitGroups({ data, onSetBoundaries, ...actions }: GroupActions & {
  data: SeriesUnits;
  onSetBoundaries: () => void;
}) {
  const form: "volumes" | "chapters" = data.form ?? (data.books.length ? "volumes" : "chapters");
  const preference = data.preference ?? "volumes";
  const uniform = data.uniform ?? "mixed";
  const hasProblem = data.books.some(isProblem);
  const [filter, setFilter] = useState<UnitFilter>("all");
  const [order, setOrder] = useState<UnitOrder>("ascending");
  const books = useMemo(() => {
    const matching = data.books.map((book) => ({ book, chapters: book.chapters.filter((chapter) => matchesChapter(chapter, filter === "problems" ? "all" : filter)) }))
      .filter(({ book, chapters }) => matchesBook(book, filter) || (filter !== "problems" && chapters.length > 0));
    return orderedRows(matching, order);
  }, [data.books, filter, order]);
  const allChapters = useMemo(() => {
    const merged = [...data.books.flatMap((book) => book.chapters), ...data.unassigned_chapters];
    return merged.filter((chapter) => matchesChapter(chapter, filter === "problems" ? "missing" : filter));
  }, [data.books, data.unassigned_chapters, filter]);
  const unassigned = useMemo(() => data.unassigned_chapters.filter((chapter) => matchesChapter(chapter, filter === "problems" ? "missing" : filter)), [data.unassigned_chapters, filter]);

  const owned = data.books.filter((book) => book.owned).length;
  const assemble = data.books.filter((book) => planOf(book) === "assemble" && !book.owned).length;
  const problems = data.books.filter(isProblem).length;
  const completeBooks = uniform === "books" && data.expected_book_count !== null && data.expected_book_count > 0
    && data.books.filter((book) => book.expected && book.owned).length >= data.expected_book_count
    && data.books.every((book) => book.owned);

  if (form === "chapters") {
    const all = [...data.books.flatMap((book) => book.chapters), ...data.unassigned_chapters];
    const onDisk = all.filter((chapter) => chapter.downloaded).length;
    const ownedBooks = orderedRows(data.books.filter((book) => book.owned), order);
    return <section aria-label="Series chapters">
      <div className="toolbar">
        <h2>Chapters</h2>
        <div className="toolbar-group">
          <label>Show <select className="input" aria-label="Filter series units" value={filter} onChange={(event) => setFilter(event.target.value as UnitFilter)}>
            <option value="all">All</option><option value="missing">Missing</option><option value="downloaded">Downloaded</option><option value="monitored">Monitored</option>
          </select></label>
          <label>Number <select className="input" aria-label="Order series units" value={order} onChange={(event) => setOrder(event.target.value as UnitOrder)}>
            <option value="ascending">Ascending</option><option value="descending">Descending</option>
          </select></label>
        </div>
      </div>
      <p className="muted small units-summary">{`${onDisk} of ${all.length} chapters on disk${data.form_reason ? ` · ${data.form_reason}` : ""}.`}</p>
      <CompletenessSummary data={data} />
      {ownedBooks.length > 0 ? <section className="volume-section" aria-label="Readable books">
        <header className="volume-header"><h2>Books on disk</h2><span className="muted small">Open a book to read chapters stored inside it.</span></header>
        {ownedBooks.map((book) => <BookRow key={book.key} book={book} chapters={[]} filter={filter} order={order} {...actions} />)}
      </section> : null}
      {data.books.length > 0 ? <p className="muted small units-map"><button type="button" className="link-button" onClick={onSetBoundaries}>Edit book boundaries</button></p> : null}
      {data.warning ? <p className="muted">{data.warning}</p> : null}
      <section className="volume-section" aria-label="Chapters">
        {allChapters.length ? <ChapterTable key={filter} chapters={allChapters} book={null} order={order} renderRows={actions.renderChapterRows} /> : <p className="muted small">{filter === "all" ? "No chapters are indexed." : "No chapters match this filter."}</p>}
      </section>
    </section>;
  }

  return <section aria-label="Series books and chapters">
    <div className="toolbar">
      <h2>Books</h2>
      <div className="toolbar-group">
        <label>Show <select className="input" aria-label="Filter series units" value={filter} onChange={(event) => setFilter(event.target.value as UnitFilter)}>
          <option value="all">All</option><option value="problems">Problems only</option><option value="missing">Missing</option><option value="downloaded">Downloaded</option><option value="monitored">Monitored</option>
        </select></label>
        <label>Number <select className="input" aria-label="Order series units" value={order} onChange={(event) => setOrder(event.target.value as UnitOrder)}>
          <option value="ascending">Ascending</option><option value="descending">Descending</option>
        </select></label>
      </div>
    </div>
    <p className="muted small units-summary">
      {completeBooks ? <><b>{owned}</b> book{owned === 1 ? "" : "s"} on disk · All expected book files present</> : <>
      <b>{owned}</b> book{owned === 1 ? "" : "s"} on disk · <b>{assemble}</b> groups covered by chapters · <b>{problems}</b> incomplete
      {" · "}form: <b>{uniform === "books" ? "all books" : uniform === "chapters" ? "all chapters" : "mixed"}</b>
      {" · "}preference: <b>{preference === "chapters" ? "chapters first" : "books first"}</b>
      {data.edition_book_count !== null ? <> · Managed edition: {data.edition_book_count} books.</> : null}
      {data.form_reason ? <> · {data.form_reason}</> : null}
      </>}
    </p>
    <CompletenessSummary data={data} />
    {data.content_notes?.length ? <p className="muted small units-map">{data.content_notes.join(" ")}</p> : null}
    <p className="muted small units-map">{describeConfidence(data)} <button type="button" className="link-button" onClick={onSetBoundaries}>{completeBooks ? "Edit book boundaries" : data.mode === "flat" ? "Set boundaries" : "Correct the map"}</button>{hasProblem && filter === "all" ? <> · <button type="button" className="link-button" onClick={() => setFilter("problems")}>Problems only</button></> : null}</p>
    {data.warning ? <p className="muted">{data.warning}</p> : null}
    {books.map(({ book, chapters }) => <BookRow key={book.key} book={book} chapters={chapters} filter={filter} order={order} {...actions} />)}
    {!books.length ? <p className="muted">{filter === "all" ? "No book releases are available." : filter === "problems" ? "No problems: every book is on disk or completes from its chapters." : "No books match this filter."}</p> : null}
    {data.unassigned_chapters.length > 0 ? <section className="volume-section" aria-label="Chapters without a book assignment">
      <header className="volume-header"><h2>Chapters without a book assignment</h2><span className="muted small">{data.unassigned_chapters.filter((chapter) => chapter.downloaded).length}/{data.unassigned_chapters.length} on disk · The available chapter map does not establish which book contains these chapters.</span></header>
      {unassigned.length ? <ChapterTable key={filter} chapters={unassigned} book={null} order={order} renderRows={actions.renderChapterRows} /> : <p className="muted small">No chapters match this filter.</p>}
    </section> : null}
  </section>;
}
