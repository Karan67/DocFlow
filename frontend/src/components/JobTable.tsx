import Link from "next/link";
import type { JobSummary } from "@/lib/types";
import { PriorityBadge, StatusBadge } from "./StatusBadge";
import { relativeTime, shortId, stageLabel } from "@/lib/format";

function StageDots({ job }: { job: JobSummary }) {
  // OCR only appears for scans, so the dots show the route this job actually
  // took rather than a fixed three-step bar that is wrong half the time.
  const isDone = job.status === "DONE";
  const failedHere = job.status === "FAILED" || job.status === "DEAD_LETTER";

  return (
    <span className="inline-flex items-center gap-1 text-xs text-slate-400">
      <span className="font-mono">{stageLabel(job.stage)}</span>
      {!isDone && !failedHere && (
        <span className="text-slate-600">
          {job.stage === "extract_text" ? "· 1/3" : job.stage === "ocr" ? "· 2/3" : "· 3/3"}
        </span>
      )}
    </span>
  );
}

export function JobTable({ jobs }: { jobs: JobSummary[] }) {
  if (jobs.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-ink-700 px-4 py-10 text-center text-sm text-slate-500">
        No jobs yet. Upload a PDF above to start the pipeline.
      </div>
    );
  }

  return (
    <div className="scroll-box rounded-lg border border-ink-700 bg-ink-900">
      <table className="w-full min-w-[720px] text-left text-sm">
        <thead>
          <tr className="border-b border-ink-700 text-xs uppercase tracking-wide text-slate-500">
            <th className="px-4 py-2 font-medium">Document</th>
            <th className="px-4 py-2 font-medium">Status</th>
            <th className="px-4 py-2 font-medium">Stage</th>
            <th className="px-4 py-2 font-medium">Priority</th>
            <th className="px-4 py-2 font-medium">Retries</th>
            <th className="px-4 py-2 font-medium">Created</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr
              key={job.id}
              className="border-b border-ink-800 last:border-0 hover:bg-ink-800/60"
            >
              <td className="px-4 py-2.5">
                <Link
                  href={`/jobs/${job.id}`}
                  className="text-slate-200 hover:text-sky-300 hover:underline"
                >
                  {job.file_name}
                </Link>
                <div className="font-mono text-[11px] text-slate-600">
                  {shortId(job.id)}
                </div>
              </td>
              <td className="px-4 py-2.5">
                <StatusBadge status={job.status} />
              </td>
              <td className="px-4 py-2.5">
                <StageDots job={job} />
              </td>
              <td className="px-4 py-2.5">
                <PriorityBadge priority={job.priority} />
              </td>
              <td className="px-4 py-2.5 font-mono text-xs tabular-nums text-slate-400">
                {job.retry_count > 0 ? (
                  <span className="text-amber-300">
                    {job.retry_count}/{job.max_retries}
                  </span>
                ) : (
                  <span className="text-slate-600">0</span>
                )}
              </td>
              <td className="whitespace-nowrap px-4 py-2.5 text-xs text-slate-500">
                {relativeTime(job.created_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
