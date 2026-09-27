import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Icon, LANGUAGES, ProgressBar, Spinner, formatBytes, useApp } from "../components";
import type { ImportGroup, ImportScan, ImportState, Manga, SeriesUnit } from "../types";

const NEW_LOCAL_SERIES = "__new_local_series__";
const SUPPORTED_ARCHIVES = new Set(["cbz", "zip", "cbr", "rar", "pdf"]);
const SUPPORTED_IMAGES = new Set(["jpg", "jpeg", "png", "webp", "gif", "avif"]);
const IMPORT_NUMBER = /^\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?$/;

type UploadProgress = {
  current: string;
  filesDone: number;
  filesTotal: number;
  bytesDone: number;
  bytesTotal: number;
};

function extension(name: string) {
  return name.split(".").pop()?.toLowerCase() ?? "";
}

function supportedFile(file: File) {
  const suffix = extension(file.name);
  return SUPPORTED_ARCHIVES.has(suffix) || SUPPORTED_IMAGES.has(suffix);
}

function inferredGroupUnit(group: ImportGroup): SeriesUnit {
  return group.items.some((item) => item.chapter && !item.volume) ? "chapters" : "volumes";
}

function effectiveSeriesUnit(manga: Manga): SeriesUnit {
  if (manga.series_unit_override) return manga.series_unit_override;
  if (manga.effective_series_unit) return manga.effective_series_unit;
  return manga.library_count?.unit === "chapter" ? "chapters" : "volumes";
}

function initialNumbers(group: ImportGroup, unit: SeriesUnit) {
  return Object.fromEntries(
    group.items.map((item, index) => [
      item.path,
      String(
        (unit === "chapters" ? item.chapter || item.volume : item.volume || item.chapter) ??
          index + 1,
      ),
    ]),
  );
}

function seriesLabel(manga: Manga) {
  const year = manga.publication_year ?? manga.year;
  return `${manga.title}${year ? ` (${year})` : ""} · ${manga.preferred_language.toUpperCase()}`;
}

export default function ImportPage() {
  const { notify, refreshJobs } = useApp();
  const fileInput = useRef<HTMLInputElement | null>(null);
  const folderInput = useRef<HTMLInputElement | null>(null);
  const activeUpload = useRef<string | null>(null);
  const [scan, setScan] = useState<ImportScan | null>(null);
  const [library, setLibrary] = useState<Manga[]>([]);
  const [loadingLibrary, setLoadingLibrary] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [uploading, setUploading] = useState<UploadProgress | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [titles, setTitles] = useState<Record<string, string>>({});
  const [authors, setAuthors] = useState<Record<string, string>>({});
  const [targets, setTargets] = useState<Record<string, string>>({});
  const [units, setUnits] = useState<Record<string, SeriesUnit>>({});
  const [defaultUnits, setDefaultUnits] = useState<Record<string, SeriesUnit>>({});
  const [numbers, setNumbers] = useState<Record<string, Record<string, string>>>({});
  const [language, setLanguage] = useState("en");
  const [state, setState] = useState<ImportState | null>(null);

  const sortedLibrary = useMemo(
    () => [...library].sort((left, right) => left.title.localeCompare(right.title)),
    [library],
  );
  const libraryById = useMemo(
    () => new Map(library.map((manga) => [manga.id, manga])),
    [library],
  );

  useEffect(() => {
    if (folderInput.current) {
      folderInput.current.setAttribute("webkitdirectory", "");
      folderInput.current.setAttribute("directory", "");
    }
  }, []);

  useEffect(() => {
    api
      .library()
      .then(setLibrary)
      .catch((caught) => notify("error", String(caught)))
      .finally(() => setLoadingLibrary(false));
    api
      .importStatus()
      .then((status) => {
        if (status.running) setState(status);
      })
      .catch(() => undefined);
  }, [notify]);

  const loadScan = useCallback((result: ImportScan) => {
    const groups = result.groups.map((group) => ({ ...group, upload_id: result.upload_id }));
    setScan({ ...result, groups });
    setSelected(new Set(groups.map((group) => group.key)));
    setTitles(Object.fromEntries(groups.map((group) => [group.key, group.title])));
    setAuthors(
      Object.fromEntries(groups.map((group) => [group.key, group.authors.join(", ")])),
    );
    setTargets({});
    setUnits(Object.fromEntries(groups.map((group) => [group.key, inferredGroupUnit(group)])));
    setDefaultUnits(
      Object.fromEntries(groups.map((group) => [group.key, inferredGroupUnit(group)])),
    );
    setNumbers(
      Object.fromEntries(
        groups.map((group) => {
          const unit = inferredGroupUnit(group);
          return [group.key, initialNumbers(group, unit)];
        }),
      ),
    );
    setState((current) => (current?.running ? current : null));
  }, []);

  const discardUpload = useCallback(async (uploadId: string | null) => {
    if (!uploadId) return;
    try {
      await api.importDeleteUpload(uploadId);
    } catch {
      // The server retains resumable or already-expired sessions safely.
    }
  }, []);

  const chooseFiles = useCallback(
    async (fileList: FileList | null) => {
      const allFiles = Array.from(fileList ?? []);
      const files = allFiles.filter(supportedFile);
      if (!files.length) {
        if (allFiles.length) notify("error", "No supported archives or image files selected.");
        return;
      }
      const previousUpload = activeUpload.current;
      let uploadId: string | null = null;
      const bytesTotal = files.reduce((total, file) => total + file.size, 0);
      let bytesDone = 0;
      setScanning(false);
      setUploading({
        current: files[0].name,
        filesDone: 0,
        filesTotal: files.length,
        bytesDone: 0,
        bytesTotal,
      });
      try {
        uploadId = (await api.importCreateUpload()).upload_id;
        for (let index = 0; index < files.length; index += 1) {
          const file = files[index];
          const browserPath = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
          const relativePath =
            browserPath ||
            (SUPPORTED_IMAGES.has(extension(file.name))
              ? `Selected images/${file.name}`
              : file.name);
          setUploading({
            current: relativePath,
            filesDone: index,
            filesTotal: files.length,
            bytesDone,
            bytesTotal,
          });
          await api.importUploadFile(uploadId, relativePath, file);
          bytesDone += file.size;
        }
        const result = await api.importScanUpload(uploadId);
        loadScan(result);
        activeUpload.current = uploadId;
        if (previousUpload && previousUpload !== uploadId) void discardUpload(previousUpload);
        if (allFiles.length !== files.length) {
          notify("info", `${allFiles.length - files.length} unsupported files skipped.`);
        }
      } catch (caught) {
        await discardUpload(uploadId);
        notify("error", String(caught));
      } finally {
        setUploading(null);
        if (fileInput.current) fileInput.current.value = "";
        if (folderInput.current) folderInput.current.value = "";
      }
    },
    [discardUpload, loadScan, notify],
  );

  const runDropScan = useCallback(async () => {
    setScanning(true);
    try {
      const result = await api.importScan();
      loadScan(result);
      const previousUpload = activeUpload.current;
      activeUpload.current = null;
      void discardUpload(previousUpload);
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setScanning(false);
    }
  }, [discardUpload, loadScan, notify]);

  useEffect(() => {
    if (!state?.running) return;
    const timer = window.setInterval(async () => {
      try {
        const status = await api.importStatus();
        setState(status);
        if (!status.running) {
          window.clearInterval(timer);
          notify(
            status.errors?.length ? "info" : "success",
            `Import finished: ${status.imported ?? 0} files across ${status.series ?? 0} series` +
              (status.errors?.length ? `, ${status.errors.length} errors.` : "."),
          );
          await refreshJobs();
          const completedUpload = activeUpload.current;
          activeUpload.current = null;
          void discardUpload(completedUpload);
        }
      } catch {
        // Transient API errors should not cancel a running server-side import.
      }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [discardUpload, notify, refreshJobs, state?.running]);

  const setTarget = (group: ImportGroup, target: string) => {
    const manga = libraryById.get(target);
    const nextDefault = manga ? effectiveSeriesUnit(manga) : inferredGroupUnit(group);
    setTargets((current) => ({ ...current, [group.key]: target }));
    setDefaultUnits((current) => ({ ...current, [group.key]: nextDefault }));
    setUnits((current) => ({ ...current, [group.key]: nextDefault }));
    setNumbers((current) => ({ ...current, [group.key]: initialNumbers(group, nextDefault) }));
  };

  const updateNumber = (groupKey: string, path: string, value: string) => {
    setNumbers((current) => ({
      ...current,
      [groupKey]: { ...current[groupKey], [path]: value },
    }));
  };

  const toggle = (key: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const selectedGroups = scan?.groups.filter((group) => selected.has(group.key)) ?? [];
  const hasInvalidSelection = selectedGroups.some((group) => {
    const target = targets[group.key];
    if (!target) return true;
    if (target === NEW_LOCAL_SERIES && !(titles[group.key] ?? "").trim()) return true;
    const values = group.items.map((item) => (numbers[group.key]?.[item.path] ?? "").trim());
    return values.some((value) => !IMPORT_NUMBER.test(value)) || new Set(values).size !== values.length;
  });

  const startImport = async () => {
    if (!scan || !selectedGroups.length || hasInvalidSelection) return;
    const groups: ImportGroup[] = selectedGroups.map((group) => {
      const target = targets[group.key];
      const manga = libraryById.get(target);
      const unit = units[group.key] ?? inferredGroupUnit(group);
      return {
        ...group,
        title:
          target === NEW_LOCAL_SERIES
            ? (titles[group.key] ?? group.title).trim()
            : manga?.title ?? group.title,
        authors:
          target === NEW_LOCAL_SERIES
            ? (authors[group.key] ?? "")
                .split(",")
                .map((value) => value.trim())
                .filter(Boolean)
            : manga?.authors ?? group.authors,
        target_manga_id: target === NEW_LOCAL_SERIES ? undefined : target,
        language: manga?.preferred_language ?? language,
        unit,
        // How the files are labelled says nothing about how the series is
        // followed: that stays with the resolver (books on disk already count).
        set_series_unit: false,
        items: group.items.map((item) => {
          const number = numbers[group.key]?.[item.path]?.trim() ?? "";
          return {
            ...item,
            volume: unit === "volumes" ? number : null,
            chapter: unit === "chapters" ? number : null,
          };
        }),
      };
    });
    try {
      const status = await api.importStart(groups, language);
      setState(status);
    } catch (caught) {
      notify("error", String(caught));
    }
  };

  const running = state?.running ?? false;
  const busy = running || scanning || uploading !== null;

  return (
    <div className="page">
      <input
        ref={fileInput}
        className="visually-hidden"
        type="file"
        multiple
        accept=".cbz,.zip,.cbr,.rar,.pdf,.jpg,.jpeg,.png,.webp,.gif,.avif"
        onChange={(event) => void chooseFiles(event.target.files)}
      />
      <input
        ref={folderInput}
        className="visually-hidden"
        type="file"
        multiple
        onChange={(event) => void chooseFiles(event.target.files)}
      />

      <div className="toolbar import-toolbar">
        <h1 className="page-title">Library Import</h1>
        <div className="toolbar-group">
          <button type="button" className="btn" onClick={() => fileInput.current?.click()} disabled={busy}>
            <Icon name="upload" /> Choose files
          </button>
          <button type="button" className="btn" onClick={() => folderInput.current?.click()} disabled={busy}>
            <Icon name="library" /> Choose folder
          </button>
          <button type="button" className="btn" onClick={() => void runDropScan()} disabled={busy}>
            <Icon name="search" /> Scan drop folder
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void startImport()}
            disabled={!selectedGroups.length || hasInvalidSelection || busy}
          >
            <Icon name="upload" /> Import {selectedGroups.length || ""} selected
          </button>
        </div>
      </div>

      {uploading ? (
        <div className="panel import-progress">
          <div className="import-progress-row">
            <strong>Uploading {uploading.current}</strong>
            <span className="muted">
              {uploading.filesDone} / {uploading.filesTotal} files · {formatBytes(uploading.bytesDone)} / {formatBytes(uploading.bytesTotal)}
            </span>
          </div>
          <ProgressBar value={uploading.bytesTotal ? uploading.bytesDone / uploading.bytesTotal : 0} kind="accent" />
        </div>
      ) : null}

      {state && (state.running || state.finished) ? (
        <div className="panel import-progress">
          <div className="import-progress-row">
            <strong>{state.running ? "Importing…" : "Import finished"}</strong>
            <div className="import-progress-summary">
              <span className="muted">
                {state.done ?? 0} / {state.total ?? 0} files · {state.series ?? 0} series
              </span>
              {!state.running ? (
                <button type="button" className="btn-icon" title="Dismiss" onClick={() => setState(null)}>
                  <Icon name="close" />
                </button>
              ) : null}
            </div>
          </div>
          <ProgressBar
            value={(state.total ?? 0) > 0 ? (state.done ?? 0) / (state.total ?? 1) : 0}
            kind={state.running ? "accent" : "success"}
          />
          {state.current ? <div className="muted small">{state.current}</div> : null}
          {(state.errors ?? []).map((error, index) => (
            <div key={index} className="warn-text small">
              {error.group}{error.path ? ` · ${error.path}` : ""}: {error.error}
            </div>
          ))}
        </div>
      ) : null}

      {scanning || loadingLibrary ? (
        <Spinner />
      ) : scan === null ? (
        <div className="panel import-picker">
          <Icon name="upload" size={48} />
          <h2>Choose files or a folder</h2>
          <div className="toolbar-group">
            <button type="button" className="btn btn-primary" onClick={() => fileInput.current?.click()}>
              <Icon name="upload" /> Choose files
            </button>
            <button type="button" className="btn" onClick={() => folderInput.current?.click()}>
              <Icon name="library" /> Choose folder
            </button>
          </div>
        </div>
      ) : scan.groups.length === 0 ? (
        <div className="panel import-picker">
          <Icon name="search" size={42} />
          <h2>Nothing importable found</h2>
        </div>
      ) : (
        <>
          <div className="muted small import-root">
            {scan.root} · {scan.groups.length} candidate{scan.groups.length === 1 ? "" : "s"}
            {scan.rar_supported ? "" : " · RAR unavailable"}
            {scan.pdf_supported === false ? " · PDF unavailable" : ""}
          </div>
          {scan.groups.map((group) => {
            const target = targets[group.key] ?? "";
            const manga = libraryById.get(target);
            const unit = units[group.key] ?? inferredGroupUnit(group);
            const duplicateNumbers = new Set<string>();
            const seenNumbers = new Set<string>();
            for (const item of group.items) {
              const value = numbers[group.key]?.[item.path]?.trim() ?? "";
              if (seenNumbers.has(value)) duplicateNumbers.add(value);
              seenNumbers.add(value);
            }
            return (
              <section key={group.key} className={`panel import-group ${selected.has(group.key) ? "" : "disabled"}`}>
                <header className="import-group-heading">
                  <label className="checkbox-row">
                    <input type="checkbox" checked={selected.has(group.key)} onChange={() => toggle(group.key)} disabled={running} />
                    <strong>{group.title}</strong>
                  </label>
                  <span className="muted small">
                    {group.items.length} file{group.items.length === 1 ? "" : "s"} · {formatBytes(group.total_size)}
                  </span>
                </header>

                <div className="import-group-controls">
                  <label className="field import-target-field">
                    <span className="field-label">Target series</span>
                    <select className="input" value={target} onChange={(event) => setTarget(group, event.target.value)} disabled={!selected.has(group.key) || running}>
                      <option value="">Select a series…</option>
                      <option value={NEW_LOCAL_SERIES}>Create new local series</option>
                      {sortedLibrary.map((item) => (
                        <option key={item.id} value={item.id}>{seriesLabel(item)}</option>
                      ))}
                    </select>
                  </label>
                  <div className="field import-unit-field">
                    <span className="field-label">Import as</span>
                    <div className="import-unit-switch" role="group" aria-label="Import unit">
                      {(["chapters", "volumes"] as SeriesUnit[]).map((choice) => (
                        <button
                          key={choice}
                          type="button"
                          className={unit === choice ? "active" : ""}
                          onClick={() => setUnits((current) => ({ ...current, [group.key]: choice }))}
                          disabled={!selected.has(group.key) || running}
                        >
                          {choice === "chapters" ? "Chapters" : "Volumes"}
                        </button>
                      ))}
                    </div>
                  </div>
                  {manga ? (
                    <div className="import-target-meta muted small">
                      {manga.authors.join(", ") || "Unknown author"} · {manga.preferred_language.toUpperCase()}
                    </div>
                  ) : null}
                </div>

                {target === NEW_LOCAL_SERIES ? (
                  <div className="import-local-fields">
                    <label className="field">
                      <span className="field-label">Series title</span>
                      <input className="input" value={titles[group.key] ?? group.title} onChange={(event) => setTitles((current) => ({ ...current, [group.key]: event.target.value }))} disabled={running} />
                    </label>
                    <label className="field">
                      <span className="field-label">Authors</span>
                      <input className="input" value={authors[group.key] ?? ""} onChange={(event) => setAuthors((current) => ({ ...current, [group.key]: event.target.value }))} disabled={running} />
                    </label>
                    <label className="field import-language-field">
                      <span className="field-label">Language</span>
                      <select className="input" value={language} onChange={(event) => setLanguage(event.target.value)} disabled={running}>
                        {LANGUAGES.map(([code, label]) => <option key={code} value={code}>{label}</option>)}
                      </select>
                    </label>
                  </div>
                ) : null}

                <div className="import-items">
                  {group.items.map((item) => {
                    const value = numbers[group.key]?.[item.path] ?? "";
                    const invalid = !IMPORT_NUMBER.test(value.trim()) || duplicateNumbers.has(value.trim());
                    return (
                      <div key={item.path} className="import-item">
                        <label className="import-number-field">
                          <span className="field-label">{unit === "chapters" ? "Chapter" : "Volume"}</span>
                          <input className={`input ${invalid ? "input-error" : ""}`} inputMode="decimal" value={value} onChange={(event) => updateNumber(group.key, item.path, event.target.value)} disabled={!selected.has(group.key) || running} />
                        </label>
                        <div className="import-file-detail">
                          <strong className="import-path" title={item.path}>{item.path}</strong>
                          <span className="muted small">{item.images} pages · {formatBytes(item.size)}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </section>
            );
          })}
          {scan.skipped.length ? (
            <section className="panel import-skipped">
              <h2>Skipped</h2>
              {scan.skipped.map((item) => <div key={item.path} className="muted small">{item.path} · {item.reason}</div>)}
            </section>
          ) : null}
        </>
      )}
    </div>
  );
}
