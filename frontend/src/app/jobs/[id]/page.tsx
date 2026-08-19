import { JobDetailView } from "@/components/JobDetailView";

// Server component purely to unwrap the async params Next 15 passes, then hand
// the id to the client component that does the polling.
export default async function JobPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <JobDetailView id={id} />;
}
