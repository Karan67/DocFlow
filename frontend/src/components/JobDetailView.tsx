"use client";

import { useCallback, useState } from "react";
import Link from "next/link";
import { fetchJob } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { TERMINAL_STATUSES } from "@/lib/types";
import { PriorityBadge, StatusBadge } from "./StatusBadge";
import { StageTimeline } from "./StageTimeline";
import { relativeTime } from "@/lib/format";

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-slate-500">
        {label}
      </dt>
      <dd className="mt-0.5 text-sm text-slate-300">{value}</dd>
    </div>
  );
}

export function JobDetailView({ id }: { id: string }) {
  // The fetcher records whether the job has settled, so polling can switch
  // itself off. A dashboard left open on a finished job should go quiet rather
  // than poll the API forever.
  const [settled, setSettled] = useState(false);

  const load = useCallback(async () => {
    const job = await fetchJob(id);
    setSettled(TERMINAL_STATUSES.includes(job.status));
    return job;
  }, [id]);

  const { data: job, error } = usePolling(load, 2000, !settled);

  if (error) {
    return (
      <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
        {error}
      </div>
    );
  }

  if (!job) {
    return <div className="text-sm text-slate-500">Loading...</div>;
  }

  const result = job.result ?? {};
  const text = typeof result.text === "string" ? result.text : "";

  return (
    <main className="space-y-5">
      <Link href="/" className="text-xs text-slate-500 hover:text-slate-300">
        &larr; All jobs
      </Link>

      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-medium text-slate-100">{job.file_name}</h1>
        <StatusBadge status={job.status} />
        <PriorityBadge priority={job.priority} />
      </div>

      <dl className="grid grid-cols-2 gap-4 rounded-lg border border-ink-700 bg-ink-900 p-4 sm:grid-cols-4">
        <Field label="Job id" value={<span className="font-mono text-xs">{job.id}</span>} />
        <Field label="Created" value={relativeTime(job.created_at)} />
        <Field
          label="Retries"
          value={`${job.retry_count} / ${job.max_retries}`}
        />
        <Field
          label="Source"
          value={
            result.needed_ocr === true
              ? "OCR (no text layer)"
              : result.source === "text_layer"
                ? "Embedded text layer"
                : "-"
          }
        />
      </dl>

      <StageTimeline job={job} />

      {(job.error_message || job.status === "FAILED" || job.status === "DEAD_LETTER") && (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/5 p-4">
          <h2 className="text-sm font-medium text-rose-300">
            {job.status === "DEAD_LETTER"
              ? "Dead-lettered - retries exhausted"
              : "Failed - retrying would not help"}
          </h2>
          <pre className="scroll-box mt-2 whitespace-pre-wrap break-words text-xs text-rose-200/80">
            {job.error_message}
          </pre>
        </div>
      )}

      {job.status === "DONE" && (
        <div className="rounded-lg border border-ink-700 bg-ink-900 p-4">
          <div className="mb-3 flex flex-wrap gap-x-6 gap-y-1 text-xs text-slate-400">
            {typeof result.page_count === "number" && (
              <span>{result.page_count} pages</span>
            )}
            {typeof result.char_count === "number" && (
              <span>{result.char_count} characters</span>
            )}
            {typeof result.chunk_count === "number" && (
              <span>{result.chunk_count} vector chunks</span>
            )}
            {typeof result.embedding_model === "string" && (
              <span className="font-mono">{result.embedding_model}</span>
            )}
          </div>

          {text ? (
            <pre className="scroll-box max-h-80 whitespace-pre-wrap break-words rounded bg-ink-950 p-3 text-xs leading-relaxed text-slate-300">
              {text}
            </pre>
          ) : (
            <p className="text-xs text-slate-500">
              No text extracted. For a scan this usually means the page was
              blank.
            </p>
          )}

          {result.truncated === true && (
            <p className="mt-2 text-xs text-slate-500">
              Text truncated for storage; the full document was processed.
            </p>
          )}
        </div>
      )}
    </main>
  );
}
