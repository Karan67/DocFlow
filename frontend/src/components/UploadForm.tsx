"use client";

import { useRef, useState } from "react";
import { ApiError, uploadDocument } from "@/lib/api";
import type { JobPriority } from "@/lib/types";
import { bytes } from "@/lib/format";

type Outcome =
  | { kind: "idle" }
  | { kind: "busy" }
  | { kind: "created"; id: string }
  | { kind: "deduplicated"; id: string }
  | { kind: "error"; message: string };

export function UploadForm({ onUploaded }: { onUploaded: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [priority, setPriority] = useState<JobPriority | "">("");
  const [outcome, setOutcome] = useState<Outcome>({ kind: "idle" });
  const inputRef = useRef<HTMLInputElement>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!file) return;

    setOutcome({ kind: "busy" });
    try {
      const result = await uploadDocument(file, priority);
      setOutcome(
        result.deduplicated
          ? { kind: "deduplicated", id: result.job_id }
          : { kind: "created", id: result.job_id },
      );
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
      onUploaded();
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.status === 429
            ? "Rate limited - too many uploads in the last minute."
            : error.message
          : "Could not reach the API.";
      setOutcome({ kind: "error", message });
    }
  }

  return (
    <form
      onSubmit={submit}
      className="rounded-lg border border-ink-700 bg-ink-900 p-4"
    >
      <h2 className="text-sm font-medium text-slate-300">Upload a document</h2>

      <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-center">
        <input
          ref={inputRef}
          type="file"
          accept="application/pdf,.pdf"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          className="min-w-0 flex-1 text-sm text-slate-400 file:mr-3 file:rounded file:border-0 file:bg-ink-700 file:px-3 file:py-1.5 file:text-sm file:text-slate-200 hover:file:bg-ink-600"
        />

        <label className="flex items-center gap-2 text-sm text-slate-400">
          <span className="whitespace-nowrap">Priority</span>
          <select
            value={priority}
            onChange={(event) =>
              setPriority(event.target.value as JobPriority | "")
            }
            className="rounded border border-ink-600 bg-ink-800 px-2 py-1.5 text-sm text-slate-200"
          >
            <option value="">auto (by size)</option>
            <option value="high">high</option>
            <option value="normal">normal</option>
            <option value="low">low</option>
          </select>
        </label>

        <button
          type="submit"
          disabled={!file || outcome.kind === "busy"}
          className="rounded bg-sky-600 px-4 py-1.5 text-sm font-medium text-white transition hover:bg-sky-500 disabled:cursor-not-allowed disabled:bg-ink-700 disabled:text-slate-500"
        >
          {outcome.kind === "busy" ? "Uploading..." : "Upload"}
        </button>
      </div>

      {file && (
        <p className="mt-2 text-xs text-slate-500">
          {file.name} · {bytes(file.size)}
        </p>
      )}

      {outcome.kind === "created" && (
        <p className="mt-2 text-xs text-emerald-400">
          Queued as {outcome.id.slice(0, 8)} - watch it move through the table
          below.
        </p>
      )}
      {outcome.kind === "deduplicated" && (
        <p className="mt-2 text-xs text-slate-400">
          Identical content was already submitted, so this returned the existing
          job {outcome.id.slice(0, 8)} instead of processing it twice.
        </p>
      )}
      {outcome.kind === "error" && (
        <p className="mt-2 text-xs text-rose-400">{outcome.message}</p>
      )}

      <p className="mt-3 text-xs text-slate-500">
        PDFs only. A scan with no text layer is routed through OCR
        automatically.
      </p>
    </form>
  );
}
