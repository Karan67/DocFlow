import type { Stats } from "@/lib/types";
import { stageLabel } from "@/lib/format";

function Tile({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: string | number;
  hint?: string;
  tone?: "default" | "busy" | "warn";
}) {
  const toneClass = {
    default: "text-slate-100",
    busy: "text-sky-300",
    warn: "text-amber-300",
  }[tone];

  return (
    <div className="rounded-lg border border-ink-700 bg-ink-900 px-4 py-3">
      <div className="text-[11px] uppercase tracking-wide text-slate-500">
        {label}
      </div>
      <div className={`mt-1 font-mono text-2xl tabular-nums ${toneClass}`}>
        {value}
      </div>
      {hint && <div className="mt-0.5 text-xs text-slate-500">{hint}</div>}
    </div>
  );
}

export function StatsPanel({ stats }: { stats: Stats | null }) {
  if (!stats) {
    return (
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <div
            key={i}
            className="h-[86px] animate-pulse rounded-lg border border-ink-700 bg-ink-900"
          />
        ))}
      </div>
    );
  }

  const active =
    stats.jobs_by_status.PENDING +
    stats.jobs_by_status.PROCESSING +
    stats.jobs_by_status.RETRYING;
  const failed = stats.jobs_by_status.FAILED + stats.jobs_by_status.DEAD_LETTER;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Tile
          label="In flight"
          value={active}
          hint={
            Object.entries(stats.active_by_stage)
              .filter(([, count]) => count > 0)
              .map(([stage, count]) => `${count} ${stageLabel(stage)}`)
              .join(" · ") || "idle"
          }
          tone={active > 0 ? "busy" : "default"}
        />
        <Tile label="Done" value={stats.jobs_by_status.DONE} />
        <Tile
          label="Failed"
          value={failed}
          hint={
            stats.jobs_by_status.DEAD_LETTER > 0
              ? `${stats.jobs_by_status.DEAD_LETTER} dead-lettered`
              : "none dead-lettered"
          }
          tone={failed > 0 ? "warn" : "default"}
        />
        <Tile
          label="Vectors"
          value={stats.total_chunks}
          hint={`${stats.total_jobs} jobs total`}
        />
      </div>

      <div className="rounded-lg border border-ink-700 bg-ink-900 p-4">
        <div className="mb-3 flex items-baseline justify-between">
          <h2 className="text-sm font-medium text-slate-300">Queue depth</h2>
          <span className="text-xs text-slate-500">
            {stats.broker_reachable
              ? "waiting messages, by queue"
              : "broker unreachable"}
          </span>
        </div>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {stats.queues.map((queue) => (
            <div key={queue.name} className="rounded border border-ink-700 px-3 py-2">
              <div className="flex items-center justify-between">
                <span className="font-mono text-xs text-slate-400">
                  {queue.name}
                </span>
                {!queue.is_priority_queue && (
                  <span
                    className="rounded bg-ink-700 px-1 text-[10px] uppercase text-slate-400"
                    title="Dedicated worker pool - slow work never blocks the fast queues"
                  >
                    pool
                  </span>
                )}
              </div>
              <div
                className={`mt-1 font-mono text-xl tabular-nums ${
                  (queue.depth ?? 0) > 0 ? "text-amber-300" : "text-slate-300"
                }`}
              >
                {queue.depth ?? "-"}
              </div>
            </div>
          ))}
        </div>

        <p className="mt-3 text-xs leading-relaxed text-slate-500">
          Depth counts messages still <em>waiting</em>. Work already handed to a
          worker has been popped, so a saturated system can legitimately show
          zero here.
        </p>
      </div>
    </div>
  );
}
