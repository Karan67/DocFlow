"use client";

import { useCallback } from "react";
import { fetchJobs, fetchStats } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { StatsPanel } from "@/components/StatsPanel";
import { UploadForm } from "@/components/UploadForm";
import { JobTable } from "@/components/JobTable";

const POLL_MS = 2000;

export default function DashboardPage() {
  const {
    data: stats,
    error: statsError,
    refresh: refreshStats,
  } = usePolling(fetchStats, POLL_MS);

  const { data: jobs, refresh: refreshJobs } = usePolling(
    useCallback(() => fetchJobs(25), []),
    POLL_MS,
  );

  // Refresh immediately after an upload rather than waiting out the interval,
  // so the new job appears the moment it is accepted.
  const refreshAll = useCallback(() => {
    void refreshStats();
    void refreshJobs();
  }, [refreshStats, refreshJobs]);

  return (
    <main className="space-y-6">
      {statsError && (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
          Cannot reach the API - is the stack running? ({statsError})
        </div>
      )}

      <StatsPanel stats={stats} />

      <UploadForm onUploaded={refreshAll} />

      <section>
        <div className="mb-2 flex items-baseline justify-between">
          <h2 className="text-sm font-medium text-slate-300">Recent jobs</h2>
          <span className="flex items-center gap-1.5 text-xs text-slate-500">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
            live
          </span>
        </div>
        <JobTable jobs={jobs?.items ?? []} />
      </section>
    </main>
  );
}
