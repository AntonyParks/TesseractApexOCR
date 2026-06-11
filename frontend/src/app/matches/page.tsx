import Link from "next/link";
import { api, Match } from "@/lib/api";

export const revalidate = 60;

export default async function MatchesPage({
  searchParams,
}: {
  searchParams: { page?: string; streamer?: string };
}) {
  const page = Math.max(1, parseInt(searchParams.page ?? "1", 10));
  const limit = 50;
  const offset = (page - 1) * limit;

  let data;
  try {
    data = await api.matches({ limit, offset, streamer: searchParams.streamer });
  } catch {
    return <p style={{ color: "#f87" }}>Could not reach API.</p>;
  }

  const totalPages = Math.max(1, Math.ceil(data.total / limit));

  return (
    <>
      <div style={{ display: "flex", alignItems: "baseline", gap: "1rem", marginBottom: "1rem" }}>
        <h1 style={{ margin: 0, fontSize: "1.4rem" }}>Matches</h1>
        <span style={{ color: "#888", fontSize: "0.85rem" }}>{data.total} sessions</span>
      </div>

      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.9rem" }}>
        <thead>
          <tr style={{ borderBottom: "1px solid #333", color: "#888", textAlign: "left" }}>
            <th style={th}>Date</th>
            <th style={th}>Streamer</th>
            <th style={{ ...th, textAlign: "right" }}>Kills</th>
            <th style={{ ...th, textAlign: "right" }}>Players</th>
          </tr>
        </thead>
        <tbody>
          {data.matches.map((m: Match) => (
            <tr key={m.match_id} style={{ borderBottom: "1px solid #1a1a1a" }}>
              <td style={td}>
                <Link href={`/matches/${encodeURIComponent(m.match_id)}`} style={{ color: "#7cf", textDecoration: "none" }}>
                  {new Date(m.start_time).toLocaleString()}
                </Link>
              </td>
              <td style={{ ...td, color: "#e8d5a3" }}>{m.streamer}</td>
              <td style={{ ...td, textAlign: "right" }}>{m.kill_count}</td>
              <td style={{ ...td, textAlign: "right", color: "#aaa" }}>{m.players_observed}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div style={{ display: "flex", gap: "0.5rem", marginTop: "1.5rem", justifyContent: "center" }}>
        {page > 1 && (
          <Link href={`/matches?page=${page - 1}`} style={pageBtn}>← Prev</Link>
        )}
        <span style={{ padding: "4px 12px", color: "#888", fontSize: "0.85rem" }}>
          Page {page} / {totalPages}
        </span>
        {page < totalPages && (
          <Link href={`/matches?page=${page + 1}`} style={pageBtn}>Next →</Link>
        )}
      </div>
    </>
  );
}

const th: React.CSSProperties = { padding: "6px 8px", fontWeight: 500 };
const td: React.CSSProperties = { padding: "7px 8px" };
const pageBtn: React.CSSProperties = {
  padding: "4px 14px", borderRadius: 4, border: "1px solid #333",
  color: "#ccc", textDecoration: "none", fontSize: "0.85rem", background: "#111",
};
