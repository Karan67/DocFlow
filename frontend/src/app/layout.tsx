import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "DocFlow",
  description: "Distributed document processing pipeline",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <div className="mx-auto max-w-5xl px-4 py-8">
          <header className="mb-8 flex flex-wrap items-baseline justify-between gap-2 border-b border-ink-800 pb-4">
            <div>
              <Link
                href="/"
                className="text-lg font-semibold tracking-tight text-slate-100"
              >
                DocFlow
              </Link>
              <p className="mt-0.5 text-xs text-slate-500">
                Upload a document, workers process it in the background.
              </p>
            </div>
            <a
              href="http://localhost:5556"
              target="_blank"
              rel="noreferrer"
              className="text-xs text-slate-500 hover:text-slate-300"
            >
              Flower (worker internals) &rarr;
            </a>
          </header>

          {children}

          <footer className="mt-10 border-t border-ink-800 pt-4 text-xs text-slate-600">
            FastAPI · Celery · Redis · PostgreSQL + pgvector
          </footer>
        </div>
      </body>
    </html>
  );
}
