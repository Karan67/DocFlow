import type { JobPriority, JobStatus } from "@/lib/types";

/**
 * Colour carries meaning here, but never alone - the label is always present,
 * so the badge still reads correctly in greyscale or with colour vision
 * deficiency.
 */
const STATUS_STYLES: Record<JobStatus, string> = {
  PENDING: "bg-slate-500/15 text-slate-300 ring-slate-500/30",
  PROCESSING: "bg-sky-500/15 text-sky-300 ring-sky-500/30",
  RETRYING: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  DONE: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  FAILED: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
  DEAD_LETTER: "bg-fuchsia-500/15 text-fuchsia-300 ring-fuchsia-500/30",
};

const STATUS_LABELS: Record<JobStatus, string> = {
  PENDING: "Pending",
  PROCESSING: "Processing",
  RETRYING: "Retrying",
  DONE: "Done",
  FAILED: "Failed",
  DEAD_LETTER: "Dead letter",
};

export function StatusBadge({ status }: { status: JobStatus }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_STYLES[status]}`}
    >
      {(status === "PROCESSING" || status === "RETRYING") && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
      )}
      {STATUS_LABELS[status]}
    </span>
  );
}

const PRIORITY_STYLES: Record<JobPriority, string> = {
  high: "bg-orange-500/15 text-orange-300 ring-orange-500/30",
  normal: "bg-ink-700 text-slate-400 ring-ink-600",
  low: "bg-ink-800 text-slate-500 ring-ink-700",
};

export function PriorityBadge({ priority }: { priority: JobPriority }) {
  return (
    <span
      className={`inline-flex rounded px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide ring-1 ring-inset ${PRIORITY_STYLES[priority]}`}
    >
      {priority}
    </span>
  );
}
