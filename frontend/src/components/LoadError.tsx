export function LoadError({
  message,
  retryLabel,
  retry,
  loading = false,
  hasData = false,
}: {
  message: string;
  retryLabel: string;
  retry: () => void;
  loading?: boolean;
  hasData?: boolean;
}) {
  return (
    <div className="banner banner-danger load-error" role="alert">
      <span>{hasData ? "Showing the last loaded data. " : ""}{message}</span>
      <button type="button" className="btn" disabled={loading} onClick={retry}>
        {retryLabel}
      </button>
    </div>
  );
}
