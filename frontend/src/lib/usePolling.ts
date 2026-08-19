"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Poll an endpoint on an interval.
 *
 * Polling rather than websockets is a deliberate match for the backend: the
 * API's contract is "ask me for the current status", and job status lives in
 * Postgres. Set `active` to false once a job reaches a terminal state - there
 * is nothing further to see, and a dashboard left open should not hammer the
 * API forever.
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  active = true,
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Kept in a ref so a caller passing an inline arrow function does not
  // restart the interval on every render.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const refresh = useCallback(async () => {
    try {
      const next = await fetcherRef.current();
      setData(next);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Request failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const tick = () => {
      if (!cancelled) void refresh();
    };

    tick();
    if (!active) return () => {
      cancelled = true;
    };

    const handle = setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
  }, [refresh, intervalMs, active]);

  return { data, error, loading, refresh };
}
