import type { JobDetail, StageEntry } from "@/lib/types";
import { duration, stageLabel } from "@/lib/format";

const DOT_STYLES: Record<string, string> = {
  DONE: "bg-emerald-400",
  FAILED: "bg-rose-400",
  DEAD_LETTER: "bg-fuchsia-400",
};

function detailLine(entry: StageEntry): string | null {
  const detail = entry.detail;
  if (!detail) return null;

  if (typeof detail.error === "string") return detail.error;
  if (typeof detail.decision === "string") return detail.decision;
  if (typeof detail.skipped === "string") return `skipped: ${detail.skipped}`;

  const parts: string[] = [];
  if (typeof detail.page_count === "number") parts.push(`${detail.page_count} pages`);
  if (typeof detail.char_count === "number") parts.push(`${detail.char_count} chars`);
  if (typeof detail.chunk_count === "number") parts.push(`${detail.chunk_count} chunks`);
  return parts.length > 0 ? parts.join(" · ") : null;
}

/**
 * The stage timeline is what makes a mid-pipeline failure legible: the job
 * status says it failed, this says which step failed and what the earlier ones
 * produced.
 */
export function StageTimeline({ job }: { job: JobDetail }) {
  const pending =
    !["DONE", "FAILED", "DEAD_LETTER"].includes(job.status) &&
    !job.stages.some((entry) => entry.stage === job.stage);

  return (
    <div className="rounded-lg border border-ink-700 bg-ink-900 p-4">
      <h2 className="text-sm font-medium text-slate-300">Pipeline</h2>

      <ol className="mt-4 space-y-0">
        {job.stages.map((entry, index) => {
          const attempts = Number(entry.detail?.attempts ?? 1);
          return (
            <li key={`${entry.stage}-${index}`} className="flex gap-3">
              <div className="flex flex-col items-center">
                <span
                  className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${
                    DOT_STYLES[entry.status] ?? "bg-slate-500"
                  }`}
                />
                {(index < job.stages.length - 1 || pending) && (
                  <span className="w-px flex-1 bg-ink-700" />
                )}
              </div>

              <div className="flex-1 pb-5">
                <div className="flex flex-wrap items-baseline gap-x-3">
                  <span className="text-sm font-medium text-slate-200">
                    {stageLabel(entry.stage)}
                  </span>
                  <span className="font-mono text-xs tabular-nums text-slate-400">
                    {duration(entry.duration_ms)}
                  </span>
                  {attempts > 1 && (
                    <span className="rounded bg-amber-500/15 px-1.5 text-[11px] text-amber-300">
                      {attempts} attempts
                    </span>
                  )}
                  {entry.status !== "DONE" && (
                    <span className="text-xs text-rose-300">{entry.status}</span>
                  )}
                </div>
                {detailLine(entry) && (
                  <p className="mt-0.5 break-words text-xs text-slate-500">
                    {detailLine(entry)}
                  </p>
                )}
              </div>
            </li>
          );
        })}

        {pending && (
          <li className="flex gap-3">
            <span className="mt-1.5 h-2.5 w-2.5 shrink-0 animate-pulse rounded-full bg-sky-400" />
            <div className="flex-1">
              <span className="text-sm font-medium text-slate-200">
                {stageLabel(job.stage)}
              </span>
              <p className="mt-0.5 text-xs text-slate-500">
                {job.status === "RETRYING"
                  ? `retrying${job.next_retry_at ? ` at ${new Date(job.next_retry_at).toLocaleTimeString()}` : ""}`
                  : job.status === "PENDING"
                    ? "queued"
                    : "running"}
              </p>
            </div>
          </li>
        )}
      </ol>

      {job.stages.length === 0 && !pending && (
        <p className="text-xs text-slate-500">No stages recorded.</p>
      )}
    </div>
  );
}
