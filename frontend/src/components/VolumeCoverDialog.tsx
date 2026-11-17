import { useEffect, useState } from "react";
import { Cover, Icon, Modal } from "../components";
import { t } from "../i18n";

const MAX_ARTWORK_UPLOAD_BYTES = 20 * 1024 * 1024;
const SUPPORTED_ARTWORK_UPLOAD_TYPES = new Set([
  "image/jpeg",
  "image/jpg",
  "image/png",
  "image/webp",
  "image/gif",
  "image/avif",
]);

export default function VolumeCoverDialog({
  volume,
  coverUrl,
  manual,
  saving,
  onClose,
  onUpload,
  onRemove,
  onReject,
}: {
  volume: string;
  coverUrl: string | null;
  manual: boolean;
  saving: boolean;
  onClose: () => void;
  onUpload: (file: File) => void;
  onRemove: () => void;
  onReject: (message: string) => void;
}) {
  const [pendingUpload, setPendingUpload] = useState<File | null>(null);
  const [pendingPreview, setPendingPreview] = useState<string | null>(null);

  useEffect(() => () => {
    if (pendingPreview) URL.revokeObjectURL(pendingPreview);
  }, [pendingPreview]);

  const chooseUpload = (file: File | undefined) => {
    if (!file) return;
    if (file.size > MAX_ARTWORK_UPLOAD_BYTES) {
      onReject(t("The cover image exceeds the 20 MiB safety limit."));
      return;
    }
    if (file.type && !SUPPORTED_ARTWORK_UPLOAD_TYPES.has(file.type.toLowerCase())) {
      onReject(t("Choose a JPEG, PNG, WebP, GIF, or AVIF image."));
      return;
    }
    if (pendingPreview) URL.revokeObjectURL(pendingPreview);
    setPendingUpload(file);
    setPendingPreview(URL.createObjectURL(file));
  };

  return (
    <Modal
      title={t("Choose cover · Volume {volume}", { volume })}
      onClose={saving ? () => undefined : onClose}
      footer={
        <>
          {manual ? (
            <button type="button" className="btn" disabled={saving} onClick={onRemove}>
              <Icon name="trash" /> {saving ? t("Publishing…") : t("Remove custom cover")}
            </button>
          ) : null}
          <button type="button" className="btn" disabled={saving} onClick={onClose}>
            {t("Cancel")}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={saving || pendingUpload === null}
            onClick={() => pendingUpload && onUpload(pendingUpload)}
          >
            <Icon name="check" /> {saving ? t("Publishing…") : t("Save cover")}
          </button>
        </>
      }
    >
      <p className="muted">
        {t("A cover chosen here, or a poster saved beside the book with the same name, is used instead of the catalogue and the archive's first page. Metadata refresh keeps that choice. Uploaded images are fitted to a 1000×1500 JPEG without stretching or cropping.")}
      </p>
      <div className="artwork-upload-row">
        {pendingPreview ? (
          <img className="volume-cover" src={pendingPreview} alt={t("Custom cover preview")} />
        ) : (
          <Cover url={coverUrl} title={t("Volume {number}", { number: volume })} className="volume-cover" />
        )}
        <label className={`btn artwork-upload-button ${saving ? "disabled" : ""}`} aria-disabled={saving}>
          <Icon name="upload" /> {t("Upload image")}
          <input
            type="file"
            hidden
            disabled={saving}
            accept="image/jpeg,image/png,image/webp,image/gif,image/avif"
            aria-label={t("Upload cover for volume {volume}", { volume })}
            onChange={(event) => {
              chooseUpload(event.currentTarget.files?.[0]);
              event.currentTarget.value = "";
            }}
          />
        </label>
        <span className="muted small">
          {pendingUpload
            ? t("{name} · will become 1000×1500", { name: pendingUpload.name })
            : t("JPEG, PNG, WebP, GIF or AVIF · maximum 20 MiB. The original aspect ratio is preserved; margins are added when needed.")}
        </span>
      </div>
    </Modal>
  );
}
