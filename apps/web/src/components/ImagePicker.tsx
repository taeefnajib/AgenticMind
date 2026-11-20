import { useRef, useState } from "react";

import { api } from "../api/client";
import type { ArtifactInfo } from "../api/types";
import { useWorkbench } from "../lib/workbench";

/** Extensions an Image Input accepts (mirrors agenticmind_node_sdk.images.IMAGE_MEDIA_TYPES; no SVG). */
export const IMAGE_ACCEPT = ".png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff";

export interface ImageRef {
  kind: "image";
  artifact_id: string;
  filename?: string;
  media_type: string;
  size?: number;
}

export function isImageRef(value: unknown): value is ImageRef {
  return (
    !!value &&
    typeof value === "object" &&
    (value as { kind?: unknown }).kind === "image" &&
    typeof (value as { artifact_id?: unknown }).artifact_id === "string"
  );
}

export function isImageFile(f: Pick<ArtifactInfo, "media_type">): boolean {
  return f.media_type.startsWith("image/") && f.media_type !== "image/svg+xml";
}

/** A thumbnail of an uploaded image (TIFF/BMP may not render in every browser: the name is shown then). */
export function ImageThumb({ id, name, size = "md" }: { id: string; name?: string; size?: "sm" | "md" | "lg" }) {
  const { files } = useWorkbench();
  const [broken, setBroken] = useState(false);
  const cls = { sm: "h-10 w-10", md: "h-16 w-16", lg: "h-40 max-w-full" }[size];
  if (!files.projectId) return null;
  if (broken) {
    return (
      <span className={`inline-flex ${size === "lg" ? "h-10 px-2" : cls} items-center justify-center rounded-md border border-slate-200 bg-slate-50 text-[10px] text-slate-500`}>
        {name ?? "image"}
      </span>
    );
  }
  return (
    <img
      src={api.fileUrl(files.projectId, id)}
      alt={name ?? "uploaded image"}
      title={name}
      className={`${cls} rounded-md border border-slate-200 object-cover`}
      onError={() => setBroken(true)}
    />
  );
}

/** Pick an uploaded image or upload a new one; the value is the file ID (what an Image Input reads). */
export function ImagePicker({
  id,
  value,
  onChange,
  compact = false,
}: {
  id?: string;
  value: string | undefined;
  onChange: (fileId: string | undefined) => void;
  compact?: boolean;
}) {
  const { files } = useWorkbench();
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const images = files.files.filter(isImageFile);
  const selected = images.find((f) => f.id === value);

  const upload = async (list: File[]) => {
    if (list.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const [added] = await files.upload(list);
      if (added) onChange(added.id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={compact ? "flex items-center gap-2" : "space-y-1.5"}>
      <div className="flex items-center gap-2">
        {value && <ImageThumb id={value} name={selected?.filename} size={compact ? "sm" : "md"} />}
        <div className="min-w-0 flex-1 space-y-1">
          <select
            id={id}
            className="am-input !py-1 text-xs"
            value={value ?? ""}
            onChange={(e) => onChange(e.target.value || undefined)}
            aria-label="Choose an uploaded image"
          >
            <option value="">{images.length ? "Choose an image…" : "No images uploaded yet"}</option>
            {images.map((f) => (
              <option key={f.id} value={f.id}>
                {f.filename}
              </option>
            ))}
          </select>
          <div className="flex gap-1.5">
            <button type="button" className="am-btn !px-2 !py-0.5 text-xs" onClick={() => input.current?.click()} disabled={busy || !files.projectId}>
              {busy ? "Uploading…" : "Upload image"}
            </button>
            {value && (
              <button type="button" className="am-btn !px-2 !py-0.5 text-xs" onClick={() => onChange(undefined)}>
                Clear
              </button>
            )}
          </div>
        </div>
      </div>
      <input
        ref={input}
        type="file"
        accept={IMAGE_ACCEPT}
        className="hidden"
        onChange={(e) => {
          // copy before clearing: clearing the input empties its live FileList
          const picked = e.target.files ? Array.from(e.target.files) : [];
          e.target.value = "";
          void upload(picked);
        }}
      />
      {error && <p className="text-[11px] text-red-600">{error}</p>}
    </div>
  );
}
