// Start-form field for mockups and reference images: drag, browse or paste (Ctrl+V). Each image is uploaded as it is added;
// the form value is the list of upload ids (the run keeps its own copies).
import { ImagePlus, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { cn } from "@/lib/format";

const MAX_IMAGES = 6;
const MAX_BYTES = 5 * 1024 * 1024;
const TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];

type Item = { id: string; name: string; url: string };

export function ImagesField({ value, onChange }: { value: string[]; onChange: (ids: string[]) => void }) {
  const [items, setItems] = useState<Item[]>([]);
  const [busy, setBusy] = useState(0);
  const [error, setError] = useState("");
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const ids = useRef<string[]>(value);
  ids.current = value;

  useEffect(() => {
    if (!value.length) setItems([]);
  }, [value]);

  const add = async (files: File[]) => {
    setError("");
    const room = MAX_IMAGES - ids.current.length;
    const ok = files.filter((f) => TYPES.includes(f.type));
    if (ok.length < files.length) setError("Only png, jpg, gif and webp images are accepted.");
    if (ok.length > room) setError(`At most ${MAX_IMAGES} images per run (each one costs tokens in every step that reads it).`);
    for (const f of ok.slice(0, Math.max(room, 0))) {
      if (f.size > MAX_BYTES) {
        setError(`${f.name} is over 5 MB.`);
        continue;
      }
      setBusy((n) => n + 1);
      try {
        const data = await new Promise<string>((res, rej) => {
          const r = new FileReader();
          r.onload = () => res(String(r.result));
          r.onerror = () => rej(new Error("could not read the file"));
          r.readAsDataURL(f);
        });
        const up = await api.uploadImage(f.name || "pasted.png", data);
        setItems((cur) => [...cur, { id: up.id, name: up.name, url: data }]);
        ids.current = [...ids.current, up.id];
        onChange(ids.current);
      } catch (e) {
        setError(e instanceof Error ? e.message : "upload failed");
      } finally {
        setBusy((n) => n - 1);
      }
    }
  };

  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const files = [...(e.clipboardData?.files ?? [])].filter((f) => f.type.startsWith("image/"));
      if (files.length) {
        e.preventDefault();
        void add(files);
      }
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const remove = (id: string) => {
    setItems((cur) => cur.filter((i) => i.id !== id));
    ids.current = ids.current.filter((x) => x !== id);
    onChange(ids.current);
  };

  return (
    <div data-testid="images-field">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          void add([...e.dataTransfer.files]);
        }}
        onClick={() => input.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && input.current?.click()}
        className={cn(
          "flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-dashed px-3 py-4 text-xs text-muted-foreground transition-colors",
          over ? "border-primary bg-primary-soft text-primary" : "border-border hover:text-foreground",
        )}
      >
        <ImagePlus className="size-4" />
        {busy ? "Uploading…" : "Drop images here, click to browse, or paste with Ctrl+V"}
        <input ref={input} type="file" accept={TYPES.join(",")} multiple hidden onChange={(e) => { void add([...(e.target.files ?? [])]); e.target.value = ""; }} />
      </div>
      {items.length ? (
        <div className="mt-2 flex flex-wrap gap-2">
          {items.map((i) => (
            <div key={i.id} className="group relative h-20 w-28 overflow-hidden rounded-md border border-border bg-surface" title={i.name}>
              <img src={i.url} alt={i.name} className="h-full w-full object-cover" />
              <button type="button" aria-label={`Remove ${i.name}`} onClick={() => remove(i.id)} className="absolute right-1 top-1 rounded bg-background/80 p-0.5 text-foreground opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100">
                <X className="size-3" />
              </button>
            </div>
          ))}
        </div>
      ) : null}
      {error ? <p className="mt-1 text-xs text-destructive">{error}</p> : null}
    </div>
  );
}
